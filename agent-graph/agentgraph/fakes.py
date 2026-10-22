"""Stand-ins for Opus, GitHub and Telegram. Used by tests and `demo`, so the graph runs end to end with no keys."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .jev import FakeJev, JevAnswer


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def send(self, text, gate_id=None):
        self.sent.append((text, gate_id))


class FakeOpus:
    """Bumps the add-on version and writes a CHANGELOG entry, the most common issue on this repo."""

    def __init__(self, review_approve=True, broken_first_write=False):
        self.review_approve, self.broken_first_write, self.calls = review_approve, broken_first_write, []

    def _u(self, kind):
        self.calls.append(kind)
        return {"kind": kind, "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.008, "latency_ms": 900.0,
                "stop_reason": "end_turn"}

    def plan(self, issue, addon, route, files, listing, feedback):
        return {"summary": f"Bump {addon} version and log it", "steps": ["bump version", "changelog"],
                "files": [{"path": f"{addon}/config.yaml", "action": "modify", "why": "version"},
                          {"path": f"{addon}/CHANGELOG.md", "action": "modify", "why": "entry"}],
                "version_bump": True}, self._u("plan")

    def write(self, issue, plan, files, feedback):
        out = []
        for path, text in files.items():
            if path.endswith("config.yaml"):
                m = re.search(r"^version:\s*\"?([\d.]+)\"?", text, re.M)
                major, minor, patch = (m.group(1).split(".") + ["0", "0"])[:3]
                self.new_version = f"{major}.{minor}.{int(patch) + 1}"
                text = text.replace(m.group(0), f"version: {self.new_version}")
            out.append({"path": path, "content": text})
        for f in out:
            if f["path"].endswith("CHANGELOG.md"):
                bad = self.broken_first_write and "write" not in self.calls
                v = "0.0.0" if bad else self.new_version
                f["content"] = f"# Changelog\n\n## {v}\n\n- {issue['title']}\n\n" + f["content"].split("\n", 1)[-1].lstrip("\n")
        return {"files": out, "commit_message": f"Bump version for issue #{issue['issue_number']}"}, self._u("write")

    def review(self, issue, plan, diff):
        probs = [] if self.review_approve else [{"severity": "blocking", "cause": "code", "text": "wrong"}]
        return {"approve": self.review_approve, "summary": "Version bump with changelog entry.", "problems": probs}, self._u("review")

    def arbitrate(self, question, options, state, probs):
        best = max(probs, key=probs.get) if probs else options[0]
        if question["type"] == "noul":
            best = "yes" if probs.get("yes", 0.5) >= 0.5 else "no"
        return {"answer": best if best in options else options[0], "reason": "fake"}, self._u("arbitrate")

    def improve(self, cfg, report):
        return {"root_causes": ["route wording too vague"],
                "questions": [{"id": "route", "instructions": cfg["questions"]["route"]["instructions"] + " Pick one."}],
                "thresholds": [{"name": "choice_shard", "value": cfg["thresholds"]["choice_shard"] + 0.01}],
                "notes": "small wording change"}, self._u("improve")


class FakeGitHub:
    def __init__(self, remote: Path, issues: list[dict], ci: str = "green"):
        self.remote, self.issues, self.ci = Path(remote), issues, ci
        self.prs, self.merged = {}, []

    def labeled_issues(self, label):
        return self.issues

    def _sha(self, branch):
        return subprocess.run(["git", "rev-parse", branch], cwd=self.remote, capture_output=True, text=True).stdout.strip()

    def find_pr(self, branch):
        for n, pr in self.prs.items():
            if pr["branch"] == branch:
                return {"number": n, "sha": self._sha(branch), "url": f"https://example/pr/{n}"}
        return None

    def open_pr(self, branch, base, title, body):
        n = 100 + len(self.prs)
        self.prs[n] = {"branch": branch, "title": title, "body": body}
        return {"number": n, "sha": self._sha(branch), "url": f"https://example/pr/{n}"}

    def pr_head(self, number):
        return self._sha(self.prs[number]["branch"])

    def ci_state(self, sha):
        return (self.ci, "fake")

    def merge(self, ticket):
        from .github import GateTicket

        if not isinstance(ticket, GateTicket):
            raise PermissionError("merge needs a GateTicket")
        self.merged.append(ticket.payload["pr_number"])
        return {"merged": True}


def make_remote(base: Path, addon: str = "ssh", version: str = "9.1.0") -> Path:
    """Bare repo with one add-on, standing in for kingston2020/addons."""
    src, remote = base / "seed", base / "remote.git"
    (src / addon).mkdir(parents=True)
    (src / addon / "config.yaml").write_text(f"---\nname: Terminal & SSH\nversion: {version}\nslug: ssh\n")
    (src / addon / "CHANGELOG.md").write_text(f"# Changelog\n\n## {version}\n\n- Previous release\n")
    (src / "duckdns").mkdir()
    (src / "duckdns" / "config.yaml").write_text("---\nname: Duck DNS\nversion: 1.0.0\nslug: duckdns\n")
    run = lambda *a, cwd=src: subprocess.run(a, cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "-b", "master")
    run("git", "add", "-A")
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    run("git", "clone", "-q", "--bare", str(src), str(remote), cwd=base)
    return remote


def demo_jev(addon: str = "ssh") -> FakeJev:
    return FakeJev({
        "addon": JevAnswer("choice", {addon: 0.94, "duckdns": 0.06}, addon, 0.94),
        "route": JevAnswer("choice", {"version_bump": 0.71, "config_change": 0.22, "docs_change": 0.05, "needs_human": 0.02},
                           "version_bump", 0.71),  # unclear on purpose: goes to Opus
        "plan_complete": JevAnswer("noul", {"yes": 0.96, "no": 0.04}, 0.96, None),
        "retry_helps": JevAnswer("noul", {"yes": 0.8, "no": 0.2}, 0.8, None),
        "diff_risk": JevAnswer("score", {"0": 0.2, "1": 0.75, "2": 0.05, "3": 0.0, "4": 0.0}, 0.2125, 0.86),
        "is_done": JevAnswer("noul", {"yes": 0.97, "no": 0.03}, 0.97, None),
    })
