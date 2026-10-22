"""Jev: the fast reflex. One call per fork, all of the node's questions at once, probabilities only."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

JEV_PRICE_PER_M_INPUT = 0.042  # USD, output is free (TypeSafe pricing via OpenRouter)
OPENROUTER_BASE = "https://openrouter.ai/api"


class JevUnavailable(Exception):
    """Timeout, network or API error. The router sends every fork in the call to Opus."""


@dataclass
class JevAnswer:
    qtype: str
    probs: dict[str, float]   # option -> probability (noul: yes/no, score: level index)
    value: object             # choice: option, noul: p(yes), score: expected level normalised to 0..1
    confidence: float | None


@dataclass
class JevResult:
    answers: dict[str, JevAnswer]
    latency_ms: float
    cost_usd: float


def build_questions(qcfg: dict[str, dict], dynamic: dict[str, dict]) -> dict[str, dict]:
    """graph.md questions -> Jev wire questions. Option sets are fixed by graph.md or by code (dynamic:*)."""
    out = {}
    for qid, q in qcfg.items():
        wire = {"type": q["type"], "instructions": q["instructions"]}
        crit = q.get("criteria")
        if isinstance(crit, str) and crit.startswith("dynamic:"):
            crit = dynamic[crit.split(":", 1)[1]]
        if q["type"] in ("choice", "score"):
            wire["criteria"] = crit
        out[qid] = wire
    return out


def normalise(qtype: str, ans, levels: int | None = None) -> JevAnswer:
    if qtype == "noul":
        p = float(ans.noul)
        return JevAnswer("noul", {"yes": p, "no": 1 - p}, p, None)
    if qtype == "choice":
        return JevAnswer("choice", dict(ans.probabilities), ans.choice, float(ans.confidence))
    if qtype == "score":
        n = levels or len(ans.probabilities)
        val = float(ans.score) / max(n - 1, 1)
        return JevAnswer("score", {str(k): float(v) for k, v in ans.probabilities.items()}, val, float(ans.confidence))
    raise ValueError(qtype)


class Jev:
    def __init__(self, model: str, timeout_ms: int, api_key: str | None = None):
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise JevUnavailable("OPENROUTER_API_KEY missing from .env")
        self.model = model
        self.timeout_s = timeout_ms / 1000
        # Retries are code-owned: zero here, a timeout routes to Opus instead of waiting.
        self.client = TypeSafeClient(
            api_key=key, base_url=OPENROUTER_BASE, model=model,
            retry=RetryPolicy(max_retries=0), timeout=self.timeout_s,
        )

    def ask(self, state: dict | str, questions: dict[str, dict]) -> JevResult:
        from typesafe_sdk import TypeSafeError

        t0 = time.perf_counter()
        try:
            res = self.client.system_one(state=state, questions=questions)
        except TypeSafeError as e:  # covers API, timeout and connection errors
            raise JevUnavailable(type(e).__name__) from e
        latency = (time.perf_counter() - t0) * 1000
        answers = {}
        for qid, q in questions.items():
            if qid not in res.answers:
                raise JevUnavailable(f"missing answer {qid}")
            levels = len(q["criteria"]) if q["type"] == "score" else None
            answers[qid] = normalise(q["type"], res.answers[qid], levels)
        tokens = res.usage.input_tokens or 0
        return JevResult(answers, latency, tokens * JEV_PRICE_PER_M_INPUT / 1_000_000)


class FakeJev:
    """Deterministic stand-in for tests and `demo`. script: qid -> JevAnswer or callable(state)."""

    def __init__(self, script: dict, latency_ms: float = 120.0):
        self.script, self.latency_ms, self.calls = script, latency_ms, []

    def ask(self, state, questions):
        self.calls.append((json.loads(json.dumps(state, default=str)), list(questions)))
        out = {}
        for qid in questions:
            a = self.script[qid]
            a = a(state) if callable(a) else a
            if isinstance(a, Exception):
                raise a
            out[qid] = a
        return JevResult(out, self.latency_ms, 0.0000042)
