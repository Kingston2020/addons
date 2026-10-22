"""GitHub REST. Reads are open. Merge only accepts a GateTicket, which only the gate executor mints."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GateTicket:
    """Proof an approved gate exists. Built by gates.Executor after it re-reads the gate from the DB."""

    __slots__ = ("gate_id", "action", "payload")
    _MINT = object()

    def __init__(self, key, gate_id: int, action: str, payload: dict):
        if key is not GateTicket._MINT:
            raise PermissionError("GateTicket can only be minted by the gate executor")
        self.gate_id, self.action, self.payload = gate_id, action, payload


class GitHub:
    def __init__(self, repo: str, token: str | None = None):
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        if not self.token:
            raise GitHubError("GITHUB_TOKEN missing from .env")

    def _req(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-graph",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path} -> {e.code}: {e.read()[:300]!r}") from None

    def labeled_issues(self, label: str) -> list[dict]:
        items = self._req("GET", f"/repos/{self.repo}/issues?state=open&labels={label}&per_page=50")
        return [
            {"issue_number": i["number"], "title": i["title"], "body": i.get("body") or "",
             "labels": [lb["name"] for lb in i.get("labels", [])], "author": i["user"]["login"]}
            for i in items if "pull_request" not in i
        ]

    def open_pr(self, branch: str, base: str, title: str, body: str) -> dict:
        pr = self._req("POST", f"/repos/{self.repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})
        return {"number": pr["number"], "sha": pr["head"]["sha"], "url": pr["html_url"]}

    def find_pr(self, branch: str) -> dict | None:
        owner = self.repo.split("/")[0]
        prs = self._req("GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{branch}")
        if not prs:
            return None
        return {"number": prs[0]["number"], "sha": prs[0]["head"]["sha"], "url": prs[0]["html_url"]}

    def pr_head(self, number: int) -> str:
        return self._req("GET", f"/repos/{self.repo}/pulls/{number}")["head"]["sha"]

    def ci_state(self, sha: str) -> tuple[str, str]:
        """('green'|'pending'|'red', detail). Deterministic: every check run must finish clean."""
        runs = self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
        if not runs:
            return "pending", "no check runs yet"
        pending = [r["name"] for r in runs if r["status"] != "completed"]
        bad = [f"{r['name']}={r['conclusion']}" for r in runs
               if r["status"] == "completed" and r["conclusion"] not in ("success", "neutral", "skipped")]
        if bad:
            return "red", ", ".join(bad)
        if pending:
            return "pending", ", ".join(pending)
        return "green", f"{len(runs)} checks passed"

    def merge(self, ticket: GateTicket) -> dict:
        if not isinstance(ticket, GateTicket) or ticket.action != "merge":
            raise PermissionError("merge needs an approved merge GateTicket")
        p = ticket.payload
        # sha pins the merge to exactly what you approved; GitHub rejects it if the head moved.
        return self._req("PUT", f"/repos/{self.repo}/pulls/{p['pr_number']}/merge",
                         {"sha": p["sha"], "merge_method": "squash"})
