"""Deterministic routing of Jev answers. Clear answers (shard) run straight into code, unclear (split) go to Opus."""

from __future__ import annotations

from dataclasses import dataclass

from .jev import JevAnswer


@dataclass
class Verdict:
    kind: str          # shard | split
    value: object      # choice option, True/False for noul, 0..1 for score
    top_p: float


def route(a: JevAnswer, t: dict) -> Verdict:
    if a.qtype == "choice":
        ranked = sorted(a.probs.values(), reverse=True) + [0.0]
        p1, p2 = ranked[0], ranked[1]
        if p1 >= t["choice_shard"] and p1 - p2 >= t["choice_margin"]:
            return Verdict("shard", a.value, p1)
        return Verdict("split", a.value, p1)
    if a.qtype == "noul":
        p = float(a.value)
        if p >= t["noul_yes"]:
            return Verdict("shard", True, p)
        if p <= t["noul_no"]:
            return Verdict("shard", False, 1 - p)
        return Verdict("split", p >= 0.5, max(p, 1 - p))
    if a.qtype == "score":
        conf = a.confidence or 0.0
        return Verdict("shard" if conf >= t["score_confidence"] else "split", float(a.value), conf)
    raise ValueError(a.qtype)


def risk_band(risk: float, t: dict) -> str:
    """Code-owned bands. low: normal gate, mid: gate flagged HIGH CARE, high: escalate."""
    if risk < t["risk_low"]:
        return "low"
    if risk <= t["risk_high"]:
        return "mid"
    return "high"
