"""The graph runner. Nodes are loops, edges are typed handoffs, code owns every limit."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import EDGE_TYPES, HARD, Graph
from .gates import Executor
from .jev import JevUnavailable, build_questions
from .opus import OpusRefused
from .router import risk_band, route
from .workspace import Workspace, WorkspaceError, run_checks
from .github import GitHubError

log = logging.getLogger("engine")
KEY_FILES = ("config.yaml", "build.yaml", "CHANGELOG.md", "Dockerfile", "DOCS.md", "apparmor.txt")
MAX_STEPS_PER_RUN = 40


class CheckFailed(Exception):
    def __init__(self, reason: str, detail: str = "", cause: str | None = None):
        super().__init__(reason)
        self.reason, self.detail, self.cause = reason, detail or reason, cause


class Escalate(Exception):
    pass


@dataclass
class Transition:
    edge: str
    payload: dict


class Engine:
    def __init__(self, graph: Graph, store, jev, opus, gh, ws: Workspace, alerts):
        self.g, self.store, self.jev, self.opus, self.gh, self.ws, self.alerts = graph, store, jev, opus, gh, ws, alerts
        self.executor = Executor(store, gh, alerts, self)

    # ---------- triggers ----------
    def poll_github(self) -> list[int]:
        new = []
        for issue in self.gh.labeled_issues(self.g.raw["trigger_label"]):
            jid = self.store.create_job(issue["issue_number"], issue)
            if jid:
                self.store.log_event(jid, "info", f"new job for issue #{issue['issue_number']}")
                new.append(jid)
        return new

    def tick(self) -> None:
        self.executor.tick()
        for jid in self.store.runnable_jobs():
            self.run_job(jid)

    # ---------- model calls, budgeted by code ----------
    def opus_call(self, j: dict, kind: str, fn, *args):
        b = self.g.b
        if self.store.opus_calls_since(j["created_at"], j["id"]) >= b["opus_calls_per_job"]:
            raise Escalate(f"Opus budget per job ({b['opus_calls_per_job']}) used up")
        if self.store.opus_calls_since(time.time() - 86400) >= b["opus_calls_per_day"]:
            raise Escalate(f"Opus budget per day ({b['opus_calls_per_day']}) used up")
        try:
            data, usage = fn(*args)
        except OpusRefused as e:
            self.store.log_opus(job_id=j["id"], kind=kind, stop_reason=f"refused:{e}")
            raise Escalate(f"Opus stopped on {kind}: {e}") from e
        self.store.log_opus(job_id=j["id"], **usage)
        return data

    def fork(self, j: dict, node: str, state: dict, qids: list[str]) -> dict:
        """One Jev call for every question at this fork. Clear answers go straight to code, unclear to Opus."""
        qcfg = {q: self.g.raw["questions"][q] for q in qids}
        dynamic = {"addons": self.ws.addons()} if any(q.get("criteria") == "dynamic:addons" for q in qcfg.values()) else {}
        wire = build_questions(qcfg, dynamic)
        digest = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:16]
        try:
            res = self.jev.ask(state, wire)
            answers, latency, cost = res.answers, res.latency_ms, res.cost_usd / max(len(qids), 1)
            if latency > self.g.b["jev_timeout_ms"]:
                answers = None  # late answers count as timeouts: code owns the latency budget
        except JevUnavailable as e:
            self.store.log_event(j["id"], "error", f"Jev unavailable at {node}: {e}")
            answers, latency, cost = None, None, 0.0
        out = {}
        for qid in qids:
            q = wire[qid]
            if answers is None:
                kind, value, top_p, probs = "timeout", None, None, {}
            else:
                a = answers[qid]
                v = route(a, self.g.t)
                kind, value, top_p, probs = v.kind, v.value, v.top_p, a.probs
            via = "code"
            if kind != "shard":
                value = self._arbitrate(j, q, state, probs)
                via = "opus"
            self.store.log_fork(job_id=j["id"], node=node, question=qid, qtype=q["type"], probs=probs, top_p=top_p,
                                kind=kind, value=json.dumps(value), route=via, latency_ms=latency, cost_usd=cost,
                                state_digest=digest,
                                state=json.dumps(state, default=str)[:20000])
            out[qid] = value
        return out

    def _arbitrate(self, j: dict, q: dict, state: dict, probs: dict):
        if q["type"] == "noul":
            options = ["yes", "no"]
        elif q["type"] == "score":
            options = [str(i) for i in range(len(q["criteria"]))]
        else:
            options = list(q["criteria"])
        ans = self.opus_call(j, "arbitrate", self.opus.arbitrate, q, options, state, probs)["answer"]
        if ans not in options:  # schema enum makes this unreachable; code still checks
            raise Escalate(f"Opus answered outside the option set: {ans}")
        if q["type"] == "noul":
            return ans == "yes"
        if q["type"] == "score":
            return int(ans) / max(len(options) - 1, 1)
        return ans

    # ---------- the loop ----------
    def run_job(self, jid: int) -> None:
        j = self.store.job(jid)
        steps = 0
        while j["status"] == "running":
            steps += 1
            if steps > MAX_STEPS_PER_RUN:
                self._escalate(j, f"step cap {MAX_STEPS_PER_RUN} hit")
                return
            node = j["node"]
            try:
                t = NODE_FUNCS[node](self, j, dict(j["payload"]))
            except CheckFailed as e:
                try:
                    self._on_fail(j, node, e)
                except Escalate as ex:  # e.g. budget ran out during the retry decision
                    self._escalate(j, f"{node}: {ex}")
                    return
                continue
            except (Escalate, WorkspaceError, GitHubError) as e:
                self._escalate(j, f"{node}: {e}")
                return
            except Exception as e:  # noqa: BLE001 - unknown errors stop the job and page you
                log.exception("node %s crashed", node)
                self._escalate(j, f"{node} crashed: {type(e).__name__}: {e}", level="error")
                return
            self._follow(j, node, t)

    def _check_type(self, etype: str, payload: dict) -> None:
        missing = [k for k in EDGE_TYPES[etype] if k not in payload]
        if missing:
            raise Escalate(f"edge payload {etype} missing {missing}")

    def _follow(self, j: dict, node: str, t: Transition) -> None:
        e = self.g.edge(t.edge)
        if e["from"] not in (node, "*"):
            self._escalate(j, f"node {node} tried edge {t.edge} it does not own")
            return
        try:
            self._check_type(e["type"], t.payload)
        except Escalate as ex:
            self._escalate(j, str(ex))
            return
        self.store.log_edge(j["id"], e["name"], node, e["to"], e["type"], _summary(t.payload))
        j["loops_closed"].append(node)
        j["retries"][node] = 0
        if e["to"] == "human":
            self._escalate(j, t.payload.get("reason", "escalated"), logged=True)
            return
        j["payload"] = t.payload
        if e["to"].startswith("gate:"):
            action = e["to"].split(":", 1)[1]
            self.executor.request(j["id"], action, t.payload, _gate_text(t.payload), self.g.b["gate_ttl_hours"])
            j["status"], j["node"] = "awaiting_gate", e["to"]
        else:
            j["node"] = e["to"]
        self.store.save_job(j)

    def _on_fail(self, j: dict, node: str, e: CheckFailed) -> None:
        self.store.log_event(j["id"], "info", f"check failed at {node}: {e.reason}")
        if e.cause and e.cause != node:
            edge = self.g.failure_edge(node, e.cause)
            if edge:
                self._back_edge(j, edge, e)
                return
        n = j["retries"].get(node, 0) + 1
        j["retries"][node] = n
        stop = n > self.g.node(node)["max_retries"]
        if not stop and "retry_helps" in self.g.questions_for(node):
            stop = not self.fork(j, node, {"node": node, "attempt": n, "failures": e.detail[:6000]}, ["retry_helps"])["retry_helps"]
        if not stop:
            j["payload"]["feedback"] = e.detail[:8000]
            self.store.save_job(j)
            return
        edge = self.g.edge(self.g.node(node)["on_exhausted"])
        if edge["type"] == "Failure":
            self._back_edge(j, edge, e)
        else:
            self._escalate(j, f"{node} stop rule hit after {n} attempts: {e.reason}")

    def _back_edge(self, j: dict, edge: dict, e: CheckFailed) -> None:
        j["back_edges"] += 1
        if j["back_edges"] > self.g.b["max_back_edges_per_job"]:
            self._escalate(j, f"back-edge cap hit; last failure: {e.reason}")
            return
        payload = dict(j["payload"], reason=e.reason, detail=e.detail[:8000], feedback=e.detail[:8000])
        self.store.log_edge(j["id"], edge["name"], edge["from"], edge["to"], "Failure", e.reason)
        j["node"], j["payload"], j["status"] = edge["to"], payload, "running"
        j["retries"][edge["to"]] = 0
        self.store.save_job(j)

    def reopen_from_gate(self, jid: int, detail: str) -> None:
        j = self.store.job(jid)
        self._back_edge(j, self.g.edge("gate_to_code"), CheckFailed("CI red after approval", detail, cause="code"))

    def _escalate(self, j: dict, reason: str, level: str = "escalation", logged: bool = False) -> None:
        j["status"], j["note"] = "escalated", reason
        self.store.save_job(j)
        self.store.close_forks(j["id"], "miss")
        if not logged:
            self.store.log_edge(j["id"], "escalate", j["node"], "human", "Escalation", reason)
        self.store.log_event(j["id"], level, reason)
        tag = "ERROR" if level == "error" else "ESCALATION"
        self.alerts.send(f"{tag} job {j['id']} issue #{j['issue_number']} at {j['node']}:\n{reason[:800]}")


# ---------- nodes ----------

def node_intake(eng: Engine, j: dict, p: dict) -> Transition:
    for k in ("issue_number", "title", "body"):
        if k not in p:
            raise CheckFailed(f"issue state missing {k}")
    addons = eng.ws.addons()
    if not addons:
        raise CheckFailed("repo has no add-ons")
    state = {"title": p["title"], "body": p["body"][:6000], "labels": p.get("labels", [])}
    v = eng.fork(j, "intake", state, ["addon", "route"])
    if v["addon"] not in addons:
        raise CheckFailed(f"addon {v['addon']} not in repo")
    out = dict(p, addon=v["addon"], route=v["route"])
    if v["route"] == "needs_human":
        return Transition("escalate", dict(out, reason="Jev routed this issue to needs_human"))
    return Transition("intake_to_plan", out)


def node_plan(eng: Engine, j: dict, p: dict) -> Transition:
    wt, branch = eng.ws.worktree(p["issue_number"])
    addon = p["addon"]
    listing = eng.ws.listing(wt, addon)
    keys = [f"{addon}/{k}" for k in KEY_FILES] + [x for x in listing if "/rootfs/" in x][:10]
    files = eng.ws.read(wt, keys, limit=20_000)
    plan = eng.opus_call(j, "plan", eng.opus.plan, p, addon, p["route"], files, listing, p.get("feedback"))
    if not plan["files"]:
        raise CheckFailed("plan lists no files")
    if len(plan["files"]) > HARD["max_files_per_diff"]:
        raise CheckFailed(f"plan lists {len(plan['files'])} files, cap is {HARD['max_files_per_diff']}")
    for f in plan["files"]:
        try:
            eng.ws.safe_path(wt, addon, f["path"])
        except WorkspaceError as e:
            raise CheckFailed("plan path rejected", str(e)) from None
        if f["action"] == "modify" and not (wt / f["path"]).is_file():
            raise CheckFailed("plan modifies a missing file", f"{f['path']} does not exist; use create or pick another file")
    ok = eng.fork(j, "plan", {"issue": {"title": p["title"], "body": p["body"][:6000]}, "addon": addon, "plan": plan},
                  ["plan_complete"])["plan_complete"]
    if not ok:
        raise CheckFailed("plan judged incomplete", "The plan misses part of the issue or the version/CHANGELOG step.")
    out = {k: v for k, v in p.items() if k not in ("feedback", "reason", "detail")}
    return Transition("plan_to_code", dict(out, summary=plan["summary"], files=[f["path"] for f in plan["files"]],
                                           plan=plan, worktree=str(wt), branch=branch))


def node_code(eng: Engine, j: dict, p: dict) -> Transition:
    from .workspace import git

    wt, addon = Path(p["worktree"]), p["addon"]
    base_ref = f"origin/{eng.ws.base}"
    git(wt, "reset", "--quiet", "--hard", base_ref)
    git(wt, "clean", "-fdq")
    current = eng.ws.read(wt, p["files"], limit=60_000)
    patch = eng.opus_call(j, "write", eng.opus.write, p, p["plan"], current, p.get("feedback"))
    extra = [f["path"] for f in patch["files"] if f["path"] not in p["files"]]
    if extra:
        raise CheckFailed("wrote files outside the plan", f"not in plan: {extra}. Only write: {p['files']}")
    try:
        eng.ws.apply(wt, addon, patch["files"])
    except WorkspaceError as e:
        raise CheckFailed("patch rejected", str(e)) from None
    fails = run_checks(wt, addon, base_ref)
    if fails:
        plan_fault = any(
            (f.startswith("changelog:") and f"{addon}/CHANGELOG.md" not in p["files"])
            or (f.startswith("version:") and f"{addon}/config.yaml" not in p["files"])
            for f in fails
        )
        raise CheckFailed(f"{len(fails)} check(s) failed", "\n".join(fails), cause="plan" if plan_fault else None)
    diff = eng.ws.diff(wt)
    out = {k: v for k, v in p.items() if k not in ("feedback", "reason", "detail")}
    return Transition("code_to_review", dict(out, diff=diff, commit_message=patch["commit_message"][:200]))


def node_review(eng: Engine, j: dict, p: dict) -> Transition:
    state = {"issue": {"title": p["title"], "body": p["body"][:4000]}, "plan": p["summary"], "diff": p["diff"][:30000]}
    v = eng.fork(j, "review", state, ["diff_risk", "is_done"])
    risk = float(v["diff_risk"])
    band = risk_band(risk, eng.g.t)
    if band == "high":
        return Transition("escalate", dict(p, reason=f"diff risk {risk:.2f} over ceiling {eng.g.t['risk_high']}"))
    review = eng.opus_call(j, "review", eng.opus.review, p, p["plan"], p["diff"])
    blocking = [x for x in review["problems"] if x["severity"] == "blocking"]
    if blocking or not review["approve"]:
        causes = Counter(x["cause"] for x in blocking) or Counter({"code": 1})
        detail = "\n".join(f"[{x['cause']}] {x['text']}" for x in blocking) or review["summary"]
        raise CheckFailed("Opus review blocked the diff", detail, cause=causes.most_common(1)[0][0])
    if not v["is_done"]:
        raise CheckFailed("judged not done", "The diff does not fully resolve the issue or carries unrelated changes.", cause="code")
    return Transition("review_to_ship", dict(p, risk=round(risk, 3), high_care=band == "mid", review=review["summary"]))


def node_ship(eng: Engine, j: dict, p: dict) -> Transition:
    wt = Path(p["worktree"])
    msg = f"{p['commit_message']}\n\nCloses #{p['issue_number']}"
    sha = eng.ws.commit_push(wt, p["branch"], msg)
    pr = eng.gh.find_pr(p["branch"])
    if not pr:
        body = (f"Closes #{p['issue_number']}\n\nPlan: {p['summary']}\n\nReview: {p['review']}\n\n"
                f"Risk: {p['risk']}{' (HIGH CARE)' if p['high_care'] else ''}\n\nOpened by agent-graph. Merge waits for a human gate.")
        pr = eng.gh.open_pr(p["branch"], eng.ws.base, f"{p['addon']}: {p['title'][:80]}", body)
    if pr["sha"] != sha:
        raise CheckFailed("PR head does not match pushed sha", f"pushed {sha}, PR shows {pr['sha']}")
    keep = ("issue_number", "title", "addon", "summary", "risk", "high_care", "review", "worktree", "branch",
            "files", "plan", "body", "route", "commit_message")
    return Transition("ship_to_gate", dict({k: p[k] for k in keep if k in p}, pr_number=pr["number"], sha=sha,
                                           url=pr.get("url", ""), action="merge"))


NODE_FUNCS = {"intake": node_intake, "plan": node_plan, "code": node_code, "review": node_review, "ship": node_ship}


def _summary(p: dict) -> str:
    keys = ("issue_number", "addon", "route", "summary", "risk", "pr_number", "reason")
    return json.dumps({k: p[k] for k in keys if k in p}, default=str)


def _gate_text(p: dict) -> str:
    care = "HIGH CARE: read the diff before you approve.\n" if p.get("high_care") else ""
    return (f"{care}Issue #{p['issue_number']}: {p['title'][:120]}\nAdd-on: {p['addon']}\nPR #{p['pr_number']} "
            f"{p.get('url', '')}\nRisk {p['risk']}\nReview: {p.get('review', '')[:400]}\n"
            f"Approve merges sha {p['sha'][:7]} once CI is green.")
