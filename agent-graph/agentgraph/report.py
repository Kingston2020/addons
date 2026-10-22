"""Daily report: loops closed, forks answered, hours saved, Opus calls, Jev latency and cost per decision."""

from __future__ import annotations

import json
import time
from pathlib import Path


def daily(store, graph, since: float | None = None) -> dict:
    since = since or time.time() - 86400
    jobs = store.rows("SELECT * FROM jobs WHERE updated_at>=?", (since,))
    loops = 0
    minutes = 0.0
    mm = graph.raw.get("manual_minutes", {})
    for j in jobs:
        closed = json.loads(j["loops_closed"])
        loops += len(closed)
        if j["status"] == "merged":  # only count time saved on work that shipped
            minutes += sum(mm.get(n, 0) for n in set(closed))
    forks = store.one(
        "SELECT COUNT(*) n, SUM(kind='shard') shard, SUM(kind='split') split, SUM(kind='timeout') timeout, "
        "AVG(latency_ms) lat, AVG(cost_usd) cost FROM forks WHERE ts>=?", (since,))
    opus = store.one("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost FROM opus_calls WHERE ts>=?", (since,))
    status = {r["status"]: r["c"] for r in store.rows(
        "SELECT status, COUNT(*) c FROM jobs WHERE updated_at>=? GROUP BY status", (since,))}
    least = least_sure(store, since)
    return {
        "since": since,
        "jobs": status,
        "loops_closed": loops,
        "forks_answered": forks["n"] or 0,
        "forks_clear_to_code": forks["shard"] or 0,
        "forks_to_opus": (forks["split"] or 0) + (forks["timeout"] or 0),
        "jev_timeouts": forks["timeout"] or 0,
        "hours_saved": round(minutes / 60, 2),
        "opus_calls": opus["n"] or 0,
        "opus_cost_usd": round(opus["cost"] or 0, 4),
        "jev_avg_latency_ms": round(forks["lat"] or 0, 1),
        "jev_avg_cost_per_decision_usd": forks["cost"] or 0,
        "least_sure_fork": least,
    }


def least_sure(store, since: float) -> dict | None:
    r = store.one(
        "SELECT question, COUNT(*) n, AVG(top_p) p, AVG(kind!='shard') split_rate FROM forks "
        "WHERE ts>=? GROUP BY question ORDER BY split_rate DESC, p ASC LIMIT 1", (since,))
    return r if r and r["n"] else None


def render(r: dict) -> str:
    lines = [
        f"Daily report {time.strftime('%Y-%m-%d', time.localtime(r['since'] + 86400))}",
        f"Jobs: {json.dumps(r['jobs'])}",
        f"Loops closed: {r['loops_closed']}",
        f"Forks answered: {r['forks_answered']} (code {r['forks_clear_to_code']}, Opus {r['forks_to_opus']}, timeouts {r['jev_timeouts']})",
        f"Hours saved (merged work, your manual_minutes): {r['hours_saved']}",
        f"Opus calls: {r['opus_calls']} (${r['opus_cost_usd']})",
        f"Jev avg latency: {r['jev_avg_latency_ms']} ms, avg cost per decision: ${r['jev_avg_cost_per_decision_usd']:.8f}",
    ]
    if r["least_sure_fork"]:
        ls = r["least_sure_fork"]
        lines.append(f"Least sure fork: {ls['question']} (split rate {ls['split_rate']:.0%}, avg top p {ls['p'] or 0:.2f}, n={ls['n']})")
    return "\n".join(lines)


def write(store, graph, home: Path) -> tuple[dict, str]:
    r = daily(store, graph)
    text = render(r)
    out = Path(home) / "reports"
    out.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    (out / f"{day}.json").write_text(json.dumps(r, indent=2, default=str))
    (out / f"{day}.txt").write_text(text + "\n")
    return r, text
