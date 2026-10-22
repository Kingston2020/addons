"""SQLite state. The single source of truth for jobs, forks, edges, gates and model calls."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  issue_number INTEGER UNIQUE NOT NULL,
  status TEXT NOT NULL,            -- running | awaiting_gate | merged | escalated | rejected | failed
  node TEXT NOT NULL,
  payload TEXT NOT NULL,
  retries TEXT NOT NULL DEFAULT '{}',
  back_edges INTEGER NOT NULL DEFAULT 0,
  loops_closed TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS forks (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  job_id INTEGER,
  node TEXT NOT NULL,
  question TEXT NOT NULL,
  qtype TEXT NOT NULL,
  probs TEXT NOT NULL,
  top_p REAL,
  kind TEXT NOT NULL,              -- shard | split | timeout
  value TEXT,
  route TEXT NOT NULL,             -- code | opus
  latency_ms REAL,
  cost_usd REAL,
  state_digest TEXT,
  state TEXT,
  result TEXT                      -- filled when the job ends: ok | miss
);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  job_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  type TEXT NOT NULL,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS gates (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  job_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,            -- pending | approved | rejected | expired | done | failed
  decided_at REAL,
  decided_by TEXT,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS opus_calls (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  job_id INTEGER,
  kind TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  latency_ms REAL,
  stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  job_id INTEGER,
  level TEXT NOT NULL,             -- info | gate | error | escalation
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, args)

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.x(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.x(sql, args).fetchone()
        return dict(r) if r else None

    # jobs
    def create_job(self, issue_number: int, payload: dict) -> int | None:
        now = time.time()
        try:
            cur = self.x(
                "INSERT INTO jobs(issue_number,status,node,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (issue_number, "running", "intake", json.dumps(payload), now, now),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def job(self, job_id: int) -> dict:
        j = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise KeyError(job_id)
        j["payload"] = json.loads(j["payload"])
        j["retries"] = json.loads(j["retries"])
        j["loops_closed"] = json.loads(j["loops_closed"])
        return j

    def save_job(self, j: dict) -> None:
        self.x(
            "UPDATE jobs SET status=?,node=?,payload=?,retries=?,back_edges=?,loops_closed=?,updated_at=?,note=? WHERE id=?",
            (j["status"], j["node"], json.dumps(j["payload"]), json.dumps(j["retries"]), j["back_edges"],
             json.dumps(j["loops_closed"]), time.time(), j.get("note"), j["id"]),
        )

    def runnable_jobs(self) -> list[int]:
        return [r["id"] for r in self.rows("SELECT id FROM jobs WHERE status='running' ORDER BY id")]

    # logs
    def log_fork(self, **kw) -> int:
        kw.setdefault("ts", time.time())
        kw["probs"] = json.dumps(kw.get("probs", {}))
        cols = ",".join(kw)
        return self.x(f"INSERT INTO forks({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values())).lastrowid

    def log_edge(self, job_id: int, name: str, src: str, dst: str, etype: str, summary: str) -> None:
        self.x("INSERT INTO edges(ts,job_id,name,src,dst,type,summary) VALUES(?,?,?,?,?,?,?)",
               (time.time(), job_id, name, src, dst, etype, summary[:500]))

    def log_event(self, job_id: int | None, level: str, text: str) -> None:
        self.x("INSERT INTO events(ts,job_id,level,text) VALUES(?,?,?,?)", (time.time(), job_id, level, text[:2000]))

    def log_opus(self, **kw) -> None:
        kw.setdefault("ts", time.time())
        cols = ",".join(kw)
        self.x(f"INSERT INTO opus_calls({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))

    def opus_calls_since(self, since: float, job_id: int | None = None) -> int:
        if job_id is None:
            return self.one("SELECT COUNT(*) c FROM opus_calls WHERE ts>=?", (since,))["c"]
        return self.one("SELECT COUNT(*) c FROM opus_calls WHERE ts>=? AND job_id=?", (since, job_id))["c"]

    def close_forks(self, job_id: int, result: str) -> None:
        self.x("UPDATE forks SET result=? WHERE job_id=? AND result IS NULL", (result, job_id))

    # gates
    def create_gate(self, job_id: int, action: str, payload: dict, ttl_hours: float) -> int:
        now = time.time()
        return self.x(
            "INSERT INTO gates(ts,job_id,action,payload,status,expires_at) VALUES(?,?,?,?,?,?)",
            (now, job_id, action, json.dumps(payload), "pending", now + ttl_hours * 3600),
        ).lastrowid

    def gate(self, gate_id: int) -> dict | None:
        g = self.one("SELECT * FROM gates WHERE id=?", (gate_id,))
        if g:
            g["payload"] = json.loads(g["payload"])
        return g

    def decide_gate(self, gate_id: int, status: str, by: str) -> bool:
        """Only a pending, unexpired gate moves. Returns False otherwise."""
        cur = self.x(
            "UPDATE gates SET status=?,decided_at=?,decided_by=? WHERE id=? AND status='pending' AND expires_at>?",
            (status, time.time(), by, gate_id, time.time()),
        )
        return cur.rowcount == 1

    def set_gate_status(self, gate_id: int, status: str) -> None:
        self.x("UPDATE gates SET status=? WHERE id=?", (status, gate_id))

    def gates_with(self, status: str) -> list[dict]:
        out = self.rows("SELECT * FROM gates WHERE status=? ORDER BY id", (status,))
        for g in out:
            g["payload"] = json.loads(g["payload"])
        return out

    def expire_gates(self) -> list[dict]:
        out = self.rows("SELECT * FROM gates WHERE status='pending' AND expires_at<=?", (time.time(),))
        for g in out:
            self.set_gate_status(g["id"], "expired")
        return out

    # kv
    def get(self, k: str, default: str | None = None) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def put(self, k: str, v: str) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
