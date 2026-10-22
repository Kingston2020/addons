"""Final check before unattended runs. Answers the three questions and lists what could break this week."""

from __future__ import annotations

import inspect
import os
import stat
import time

from . import engine as engine_mod
from . import github as github_mod
from .config import HARD, IRREVERSIBLE, ROOT, SECRET_ENV, violations
from .report import least_sure

REQUIRED_ENV = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_TOKEN")


def final_check(graph, store=None, examples_count: int = 0) -> dict:
    cfg = graph.raw
    blockers: list[str] = []
    risks: list[str] = []

    # 1. Does every loop have a stop rule?
    stop_rules = {n: (s.get("max_retries"), s.get("on_exhausted")) for n, s in cfg["nodes"].items()}
    missing = [n for n, (r, e) in stop_rules.items() if not r or not e]
    blockers += [f"node {n} has no stop rule" for n in missing]
    if engine_mod.MAX_STEPS_PER_RUN <= 0:
        blockers.append("engine has no step cap")
    q1 = "yes" if not missing else f"no: {missing}"

    # 2. Is any hard limit owned by a model instead of code?
    model_owned = []
    bad = violations(cfg)
    blockers += bad
    if "max_retries" in str(cfg.get("questions", {})) or "budget" in str(cfg.get("questions", {})):
        model_owned.append("a Jev question mentions retries or budgets")
    src = inspect.getsource(github_mod.GitHub.merge)
    if "GateTicket" not in src:
        model_owned.append("GitHub.merge does not demand a GateTicket")
    if not IRREVERSIBLE >= {"merge", "send", "pay", "delete"}:
        model_owned.append("IRREVERSIBLE set lost an action")
    for name in ("delete_branch", "delete_repo", "close_issue", "comment"):
        if hasattr(github_mod.GitHub, name):
            model_owned.append(f"GitHub.{name} exists without a gate")
    blockers += model_owned
    q2 = "no, code owns every limit (config.HARD, GateTicket, Opus budget, step cap)" if not (model_owned or bad) else f"yes: {model_owned + bad}"

    # 3. Which fork is Jev least sure about?
    ls = least_sure(store, time.time() - 7 * 86400) if store else None
    if ls:
        q3 = f"{ls['question']}: split rate {ls['split_rate']:.0%}, avg top p {ls['p'] or 0:.2f} over {ls['n']} forks (7 days)"
        if ls["split_rate"] > 0.5:
            risks.append(f"{ls['question']} goes to Opus more than half the time; tune it or reword it")
    else:
        q3 = "unknown: no live forks yet. Run `tune` on your examples, then a supervised day."
        blockers.append("no fork history; run supervised (UNATTENDED=0) first")

    # tuning and secrets
    if not cfg.get("tuned_at"):
        blockers.append("thresholds never tuned on your examples (`python -m agentgraph tune examples/labelled.jsonl --write`)")
    if examples_count < HARD["min_tuning_examples"]:
        blockers.append(f"{examples_count} labelled examples, need {HARD['min_tuning_examples']}")
    env_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if env_missing:
        blockers.append(f".env missing {env_missing}")
    envf = ROOT / ".env"
    if envf.exists() and envf.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        blockers.append(".env readable by group/other; run chmod 600 .env")
    gi = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    if ".env" not in gi.split():
        blockers.append(".env not in .gitignore")
    leaked = [k for k in SECRET_ENV if os.environ.get(k) and os.environ[k] in (ROOT / "graph.md").read_text()]
    blockers += [f"{k} value found in graph.md" for k in leaked]

    # what could break this week
    risks += [
        "Jev latency over the timeout sends every fork to Opus; watch jev_timeouts and Opus spend in the daily report.",
        "OpenRouter Decisions API is /api/alpha; a breaking change stops all forks. Pin typesafe-sdk and model typesafe/jev-1.14.",
        "Upstream home-assistant/addons sync into the fork moves master under open PRs; the merge gate refuses a moved head.",
        "A prompt-injection issue with label `agent`: Opus sees it as data, has no tools, and the merge still waits for you. Only label issues you trust.",
        "GitHub token expiry (fine-grained PATs expire) stops pushes; set a calendar reminder for the expiry date.",
        "Check runs stuck pending keep an approved gate waiting until gate_ttl_hours, then the job expires.",
        "Daily Opus cap hit mid-job escalates the job; raise opus_calls_per_day in graph.md if this shows up.",
    ]
    if store:
        pend = store.one("SELECT COUNT(*) c FROM gates WHERE status='pending'")["c"]
        if pend > 5:
            risks.append(f"{pend} gates waiting on you; the queue grows faster than you approve.")
        err = store.one("SELECT COUNT(*) c FROM events WHERE level='error' AND ts>=?", (time.time() - 86400,))["c"]
        if err:
            risks.append(f"{err} errors in the last 24h; read them on the dashboard before going unattended.")
    return {
        "every_loop_has_stop_rule": q1,
        "model_owns_hard_limit": q2,
        "least_sure_fork": q3,
        "blockers": blockers,
        "what_could_break_this_week": risks,
        "unattended_allowed": not blockers,
    }


def render(r: dict) -> str:
    out = [
        "Does every loop have a stop rule? " + r["every_loop_has_stop_rule"],
        "Is any hard limit owned by a model? " + r["model_owns_hard_limit"],
        "Which fork is Jev least sure about? " + r["least_sure_fork"],
        "",
        "Blockers:" if r["blockers"] else "Blockers: none",
        *[f"  - {b}" for b in r["blockers"]],
        "",
        "WHAT COULD BREAK THIS WEEK?",
        *[f"  - {x}" for x in r["what_could_break_this_week"]],
        "",
        "UNATTENDED: " + ("ALLOWED" if r["unattended_allowed"] else "REFUSED until every blocker is clear"),
    ]
    return "\n".join(out)
