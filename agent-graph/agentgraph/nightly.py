"""Nightly self-improvement. Opus proposes, code clamps, replay decides. No rewrite ships until it clears the gates."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time

from .config import HARD, ROOT, read_block, violations, write_block
from .tune import score_examples

PROTECTED_THRESHOLDS = {"target_precision"}  # nightly may never move this
MAX_INSTRUCTION_CHARS = 600


def session(store, since: float) -> dict:
    forks = store.rows("SELECT ts,job_id,node,question,probs,top_p,kind,value,route,latency_ms,result,state "
                       "FROM forks WHERE ts>=? ORDER BY id", (since,))
    misses = store.rows("SELECT id,issue_number,status,node,note FROM jobs WHERE updated_at>=? "
                        "AND status IN ('escalated','rejected','failed')", (since,))
    events = store.rows("SELECT ts,job_id,level,text FROM events WHERE ts>=? AND level!='gate' ORDER BY id", (since,))
    return {"forks": forks, "misses": misses, "events": events[-300:]}


def apply_proposal(cfg: dict, prop: dict) -> tuple[dict, list[str]]:
    """Return (new cfg, rejected items). Only instructions and thresholds change, by a bounded step."""
    new, rejected = copy.deepcopy(cfg), []
    for q in prop.get("questions", []):
        qid, text = q.get("id"), str(q.get("instructions", "")).strip()
        if qid not in new["questions"]:
            rejected.append(f"unknown question {qid}")
        elif not text or len(text) > MAX_INSTRUCTION_CHARS:
            rejected.append(f"{qid}: instructions empty or over {MAX_INSTRUCTION_CHARS} chars")
        else:
            new["questions"][qid]["instructions"] = text
    step = HARD["nightly_threshold_step"]
    for t in prop.get("thresholds", []):
        name, val = t.get("name"), t.get("value")
        if name not in new["thresholds"] or name in PROTECTED_THRESHOLDS:
            rejected.append(f"threshold {name} not editable")
            continue
        old = new["thresholds"][name]
        if not isinstance(val, (int, float)) or abs(val - old) > step + 1e-9:
            rejected.append(f"{name}: {old} -> {val} exceeds step {step}")
            continue
        new["thresholds"][name] = round(float(val), 3)
    new["version"] = cfg.get("version", 1) + 1
    return new, rejected


def precision(rows: list[dict], cfg: dict) -> dict:
    t = cfg["thresholds"]
    out = {}
    for qtype, th in (("choice", t["choice_shard"]), ("noul", t["noul_yes"]), ("score", t["score_confidence"])):
        hit = [r for r in rows if r["qtype"] == qtype and r["top_p"] >= th
               and (qtype != "choice" or r["margin"] >= t["choice_margin"])]
        out[qtype] = {"clear": len(hit), "precision": (sum(r["correct"] for r in hit) / len(hit)) if hit else None}
    return out


def gates_pass(old_p: dict, new_p: dict, target: float) -> list[str]:
    fails = []
    for qtype, n in new_p.items():
        o = old_p[qtype]
        if n["precision"] is not None and n["precision"] < target:
            fails.append(f"{qtype} precision {n['precision']:.3f} under target {target}")
        if o["precision"] is not None and n["precision"] is not None and n["precision"] < o["precision"]:
            fails.append(f"{qtype} precision fell {o['precision']:.3f} -> {n['precision']:.3f}")
    return fails


def run_tests() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def nightly(store, graph, opus, jev, examples: list[dict], dynamic: dict, alerts, dry_run: bool = False) -> dict:
    since = time.time() - 86400
    cfg = graph.raw
    result = {"shipped": False, "reasons": []}
    if len(examples) < HARD["min_tuning_examples"]:
        result["reasons"].append(f"need {HARD['min_tuning_examples']} labelled examples, have {len(examples)}")
        alerts.send("Nightly rewrite skipped: " + result["reasons"][0])
        return result
    data, usage = opus.improve(cfg, session(store, since))
    store.log_opus(job_id=None, **usage)
    new, rejected = apply_proposal(cfg, data)
    result.update(root_causes=data.get("root_causes", []), rejected=rejected, notes=data.get("notes", ""))
    bad = violations(new)
    if bad:
        result["reasons"] += bad
    rows_new = score_examples(jev, new, examples, dynamic)
    rows_old = score_examples(jev, cfg, examples, dynamic)
    p_old, p_new = precision(rows_old, cfg), precision(rows_new, new)
    result.update(precision_old=p_old, precision_new=p_new)
    result["reasons"] += gates_pass(p_old, p_new, cfg["thresholds"]["target_precision"])
    if new["questions"] == cfg["questions"] and new["thresholds"] == cfg["thresholds"]:
        result["reasons"].append("no change proposed")
    if not result["reasons"] and not dry_run:
        text = graph.path.read_text()
        staged = write_block(text, new)
        staged += f"\n- v{new['version']} ({time.strftime('%Y-%m-%d')}): nightly. " + "; ".join(data.get("root_causes", []))[:500] + "\n"
        backup = text
        graph.path.write_text(staged)
        ok, out = run_tests()
        if not ok or violations(read_block(graph.path.read_text())):
            graph.path.write_text(backup)  # roll back: the same gates the code runs on
            result["reasons"].append("tests or check failed after rewrite, rolled back: " + out[-300:])
        else:
            result["shipped"] = True
    store.put("last_nightly", json.dumps(result, default=str))
    msg = "Nightly rewrite SHIPPED" if result["shipped"] else "Nightly rewrite held back"
    alerts.send(f"{msg} (v{new['version']}).\nRoot causes: {'; '.join(result.get('root_causes', []))[:600]}\n"
                f"Reasons: {'; '.join(result['reasons'])[:600] or 'all gates passed'}")
    return result
