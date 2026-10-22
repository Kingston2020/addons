"""Gates before anything irreversible. A model never opens a gate; you do, then code re-checks the world."""

from __future__ import annotations

import logging

from .config import IRREVERSIBLE
from .github import GateTicket

log = logging.getLogger("gates")


class Executor:
    def __init__(self, store, gh, alerts, engine=None):
        self.store, self.gh, self.alerts, self.engine = store, gh, alerts, engine

    def request(self, job_id: int, action: str, payload: dict, text: str, ttl_hours: float) -> int:
        if action not in IRREVERSIBLE:
            raise ValueError(f"{action} is not a gated action")
        gid = self.store.create_gate(job_id, action, payload, ttl_hours)
        self.store.log_event(job_id, "gate", f"gate {gid} {action} waiting: {text[:300]}")
        self.alerts.send(f"GATE #{gid} {action.upper()}\n{text}", gate_id=gid)
        return gid

    def decide(self, gate_id: int, status: str, who: str) -> str:
        if not self.store.decide_gate(gate_id, status, who):
            return f"Gate {gate_id} is not pending."
        self.store.log_event(None, "gate", f"gate {gate_id} {status} by {who}")
        return f"Gate {gate_id} {status}."

    def _ticket(self, gate_id: int) -> GateTicket:
        g = self.store.gate(gate_id)
        if not g or g["status"] != "approved" or g["action"] not in IRREVERSIBLE:
            raise PermissionError(f"gate {gate_id} not approved")
        return GateTicket(GateTicket._MINT, gate_id, g["action"], g["payload"])

    def tick(self) -> None:
        """Run approved gates, close rejected and expired ones. Called by the daemon loop."""
        for g in self.store.expire_gates():
            self._end_job(g["job_id"], "escalated", f"gate {g['id']} expired with no decision")
            self.alerts.send(f"Gate #{g['id']} expired. Job {g['job_id']} stopped, nothing merged.")
        for g in self.store.gates_with("rejected"):
            self.store.set_gate_status(g["id"], "done")
            self._end_job(g["job_id"], "rejected", f"gate {g['id']} rejected by you")
        for g in self.store.gates_with("approved"):
            self._run(g)

    def _run(self, g: dict) -> None:
        p = g["payload"]
        try:
            head = self.gh.pr_head(p["pr_number"])
            if head != p["sha"]:
                self.store.set_gate_status(g["id"], "failed")
                self._end_job(g["job_id"], "escalated", f"PR head moved from {p['sha'][:7]} to {head[:7]} after approval")
                self.alerts.send(f"Gate #{g['id']} cancelled: the PR changed after you approved. Nothing merged.")
                return
            ci, detail = self.gh.ci_state(p["sha"])
            if ci == "pending":
                return  # stay approved, re-check next tick
            if ci == "red":
                self.store.set_gate_status(g["id"], "failed")
                self.alerts.send(f"Gate #{g['id']}: CI red ({detail}). Sent back to the code node. Nothing merged.")
                if self.engine:
                    self.engine.reopen_from_gate(g["job_id"], f"CI failed: {detail}")
                return
            self.gh.merge(self._ticket(g["id"]))
            self.store.set_gate_status(g["id"], "done")
            self._end_job(g["job_id"], "merged", f"PR #{p['pr_number']} merged at {p['sha'][:7]}")
            self.alerts.send(f"Merged PR #{p['pr_number']} (issue #{p['issue_number']}).")
        except Exception as e:  # noqa: BLE001 - every failure is logged and alerted, never swallowed
            self.store.set_gate_status(g["id"], "failed")
            self.store.log_event(g["job_id"], "error", f"gate {g['id']} run failed: {e}")
            self.alerts.send(f"ERROR gate #{g['id']}: {str(e)[:300]}")

    def _end_job(self, job_id: int, status: str, note: str) -> None:
        j = self.store.job(job_id)
        j["status"], j["note"] = status, note
        self.store.save_job(j)
        self.store.close_forks(job_id, "ok" if status == "merged" else "miss")
        self.store.log_event(job_id, "info" if status == "merged" else "escalation", note)
