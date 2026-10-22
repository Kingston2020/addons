"""Tune thresholds on your own labelled examples. Picks the loosest threshold that still hits target precision."""

from __future__ import annotations

import json
from pathlib import Path

from .config import HARD
from .jev import build_questions

GRID = [round(0.75 + i * 0.01, 2) for i in range(25)]  # 0.75 .. 0.99


def load_examples(path: Path) -> list[dict]:
    """JSONL lines: {"question": "route", "state": {...}, "expected": "version_bump" | true | 0..1}"""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            out.append(json.loads(line))
    return out


def score_examples(jev, graph_cfg: dict, examples: list[dict], dynamic: dict) -> list[dict]:
    """Ask Jev once per example. Returns rows: question, qtype, top_p, margin, correct."""
    rows = []
    for ex in examples:
        qid = ex["question"]
        q = graph_cfg["questions"][qid]
        wire = build_questions({qid: q}, dynamic)
        a = jev.ask(ex["state"], wire).answers[qid]
        if q["type"] == "choice":
            ranked = sorted(a.probs.values(), reverse=True) + [0.0]
            rows.append({"question": qid, "qtype": "choice", "top_p": ranked[0], "margin": ranked[0] - ranked[1],
                         "correct": a.value == ex["expected"]})
        elif q["type"] == "noul":
            p = float(a.value)
            pred = p >= 0.5
            rows.append({"question": qid, "qtype": "noul", "top_p": max(p, 1 - p), "margin": None,
                         "correct": pred == bool(ex["expected"])})
        else:
            rows.append({"question": qid, "qtype": "score", "top_p": a.confidence or 0.0, "margin": None,
                         "correct": abs(float(a.value) - float(ex["expected"])) <= 0.25})
    return rows


def pick(rows: list[dict], qtype: str, target: float) -> tuple[float | None, dict]:
    """Loosest grid value whose clear answers reach target precision. Coverage = share answered by code."""
    sub = [r for r in rows if r["qtype"] == qtype]
    best, stats = None, {"n": len(sub)}
    for th in reversed(GRID):  # from strict to loose, keep loosening while precision holds
        hit = [r for r in sub if r["top_p"] >= th]
        if len(hit) < 5:
            continue
        prec = sum(r["correct"] for r in hit) / len(hit)
        if prec >= target:
            best, stats = th, {"n": len(sub), "precision": round(prec, 3), "coverage": round(len(hit) / len(sub), 3)}
        else:
            break
    return best, stats


def tune(jev, graph_cfg: dict, examples: list[dict], dynamic: dict) -> dict:
    t = graph_cfg["thresholds"]
    target = t["target_precision"]
    rows = score_examples(jev, graph_cfg, examples, dynamic)
    report = {"examples": len(rows), "target_precision": target, "suggested": {}, "stats": {}}
    for qtype, key, floor in (("choice", "choice_shard", HARD["choice_shard_floor"]),
                              ("noul", "noul_yes", HARD["noul_yes_floor"]),
                              ("score", "score_confidence", HARD["score_confidence_floor"])):
        th, stats = pick(rows, qtype, target)
        report["stats"][qtype] = stats
        if th is not None:
            report["suggested"][key] = max(th, floor)
    if "noul_yes" in report["suggested"]:
        report["suggested"]["noul_no"] = round(1 - report["suggested"]["noul_yes"], 2)
    report["enough_examples"] = len(rows) >= HARD["min_tuning_examples"]
    return report
