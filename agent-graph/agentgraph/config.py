"""Config loading. Code owns every hard limit here; graph.md only picks values inside them."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH_MD = ROOT / "graph.md"

# Ceilings and floors. No model output, no graph.md edit and no nightly rewrite moves these.
HARD = {
    "max_retries_ceiling": 5,
    "opus_calls_per_job_ceiling": 12,
    "opus_calls_per_day_ceiling": 200,
    "jev_timeout_ms_ceiling": 2000,
    "max_back_edges_ceiling": 3,
    "gate_ttl_hours_ceiling": 72,
    "choice_shard_floor": 0.75,
    "choice_margin_floor": 0.10,
    "noul_yes_floor": 0.85,
    "noul_no_ceiling": 0.15,
    "score_confidence_floor": 0.70,
    "risk_high_ceiling": 0.80,
    "target_precision_floor": 0.90,
    "nightly_threshold_step": 0.05,
    "max_files_per_diff": 10,
    "max_file_bytes": 100_000,
    "max_diff_bytes": 200_000,
    "min_tuning_examples": 20,
}

# Actions that never run without an approved gate. Not configurable.
IRREVERSIBLE = frozenset({"merge", "send", "pay", "delete"})

# Paths the agent never writes, whatever a plan says.
PROTECTED_PATHS = (".github/", ".git/", ".devcontainer/", "agent-graph/", ".hadolint.yaml")

EDGE_TYPES = {
    "IssueState": ("issue_number", "title", "body", "addon", "route"),
    "Plan": ("issue_number", "addon", "summary", "files"),
    "Diff": ("issue_number", "addon", "branch", "diff", "files"),
    "ReviewResult": ("issue_number", "addon", "branch", "risk", "high_care"),
    "ApprovalRequest": ("issue_number", "pr_number", "sha", "action"),
    "Failure": ("reason", "detail"),
    "Escalation": ("reason",),
}

NODES = ("intake", "plan", "code", "review", "ship")

SECRET_ENV = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "DASHBOARD_TOKEN")

_BLOCK = re.compile(r"<!-- graph-config:start -->\s*```json\s*(.*?)```\s*<!-- graph-config:end -->", re.S)


class ConfigError(Exception):
    pass


def load_env(path: Path | None = None) -> None:
    """Load .env into os.environ without overriding values already set."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class RedactSecrets(logging.Filter):
    """Strip any secret value from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for key in SECRET_ENV:
            val = os.environ.get(key)
            if val and len(val) >= 8 and val in msg:
                msg = msg.replace(val, "***")
        record.msg, record.args = msg, ()
        return True


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactSecrets())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def read_block(text: str) -> dict:
    m = _BLOCK.search(text)
    if not m:
        raise ConfigError("graph.md has no graph-config block")
    return json.loads(m.group(1))


def write_block(text: str, cfg: dict) -> str:
    body = json.dumps(cfg, indent=2)
    new = f"<!-- graph-config:start -->\n```json\n{body}\n```\n<!-- graph-config:end -->"
    return _BLOCK.sub(lambda _: new, text, count=1)


def violations(cfg: dict) -> list[str]:
    """Every place cfg breaks a hard limit or the graph shape. Empty list means clean."""
    out: list[str] = []
    b, t = cfg.get("budgets", {}), cfg.get("thresholds", {})

    def need(cond: bool, msg: str) -> None:
        if not cond:
            out.append(msg)

    need(0 < b.get("opus_calls_per_job", 0) <= HARD["opus_calls_per_job_ceiling"], "opus_calls_per_job outside (0, ceiling]")
    need(0 < b.get("opus_calls_per_day", 0) <= HARD["opus_calls_per_day_ceiling"], "opus_calls_per_day outside (0, ceiling]")
    need(0 < b.get("jev_timeout_ms", 0) <= HARD["jev_timeout_ms_ceiling"], "jev_timeout_ms outside (0, ceiling]")
    need(0 <= b.get("max_back_edges_per_job", -1) <= HARD["max_back_edges_ceiling"], "max_back_edges_per_job outside [0, ceiling]")
    need(0 < b.get("gate_ttl_hours", 0) <= HARD["gate_ttl_hours_ceiling"], "gate_ttl_hours outside (0, ceiling]")
    need(t.get("choice_shard", 0) >= HARD["choice_shard_floor"], "choice_shard under floor")
    need(t.get("choice_margin", 0) >= HARD["choice_margin_floor"], "choice_margin under floor")
    need(t.get("noul_yes", 0) >= HARD["noul_yes_floor"], "noul_yes under floor")
    need(t.get("noul_no", 1) <= HARD["noul_no_ceiling"], "noul_no over ceiling")
    need(t.get("score_confidence", 0) >= HARD["score_confidence_floor"], "score_confidence under floor")
    need(0 < t.get("risk_low", 0) < t.get("risk_high", 0) <= HARD["risk_high_ceiling"], "risk bands invalid or risk_high over ceiling")
    need(t.get("target_precision", 0) >= HARD["target_precision_floor"], "target_precision under floor")

    nodes = cfg.get("nodes", {})
    edges = cfg.get("edges", [])
    names = {e.get("name") for e in edges}
    need(set(nodes) == set(NODES), f"nodes must be exactly {NODES}")
    for n, spec in nodes.items():
        r = spec.get("max_retries")
        need(isinstance(r, int) and 1 <= r <= HARD["max_retries_ceiling"], f"{n}.max_retries missing or outside [1, ceiling]")
        need(spec.get("on_exhausted") in names, f"{n}.on_exhausted names no edge")
    for e in edges:
        need(e.get("type") in EDGE_TYPES, f"edge {e.get('name')} has unknown type {e.get('type')}")
        src, dst = e.get("from"), e.get("to")
        need(src == "*" or src in nodes or str(src).startswith("gate:"), f"edge {e.get('name')} from unknown node")
        need(dst in nodes or dst == "human" or str(dst).startswith("gate:"), f"edge {e.get('name')} to unknown node")
    for n in nodes:
        need(any(e.get("from") == n and e.get("type") != "Failure" for e in edges), f"node {n} has no forward edge")
    for qid, q in cfg.get("questions", {}).items():
        need(q.get("node") in nodes, f"question {qid} on unknown node")
        need(q.get("type") in ("choice", "noul", "score"), f"question {qid} has bad type")
        if q.get("type") == "score":
            need(2 <= len(q.get("criteria", [])) <= 10, f"question {qid} needs 2..10 levels")
    return out


@dataclass
class Graph:
    raw: dict
    path: Path = field(default=GRAPH_MD)

    @property
    def t(self) -> dict:
        return self.raw["thresholds"]

    @property
    def b(self) -> dict:
        return self.raw["budgets"]

    def node(self, name: str) -> dict:
        return self.raw["nodes"][name]

    def edge(self, name: str) -> dict:
        for e in self.raw["edges"]:
            if e["name"] == name:
                return e
        raise ConfigError(f"no edge {name}")

    def failure_edge(self, src: str, dst: str) -> dict | None:
        for e in self.raw["edges"]:
            if e["from"] == src and e["to"] == dst and e["type"] == "Failure":
                return e
        return None

    def questions_for(self, node: str) -> dict:
        return {k: v for k, v in self.raw["questions"].items() if v["node"] == node}


def load_graph(path: Path | None = None, strict: bool = True) -> Graph:
    path = path or GRAPH_MD
    cfg = read_block(path.read_text())
    bad = violations(cfg)
    if bad and strict:
        raise ConfigError("graph.md breaks hard limits: " + "; ".join(bad))
    return Graph(raw=copy.deepcopy(cfg), path=path)
