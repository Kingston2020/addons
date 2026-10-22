"""Telegram alerts and gate approvals via a BotFather bot. Only your chat id can approve."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.enabled = bool(self.token and self.chat_id)
        self._offset = 0

    def _call(self, method: str, body: dict, timeout: int = 15):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def send(self, text: str, gate_id: int | None = None) -> None:
        if not self.enabled:
            log.warning("telegram disabled, alert dropped: %s", text[:200])
            return
        body = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if gate_id is not None:
            body["reply_markup"] = {"inline_keyboard": [[
                {"text": "Approve", "callback_data": f"gate:{gate_id}:approve"},
                {"text": "Reject", "callback_data": f"gate:{gate_id}:reject"},
            ]]}
        try:
            self._call("sendMessage", body)
        except (urllib.error.URLError, OSError) as e:
            log.error("telegram send failed: %s", type(e).__name__)

    def poll(self, on_decision, stop: threading.Event) -> None:
        """Long-poll button taps. on_decision(gate_id, 'approved'|'rejected', who) -> str reply."""
        while self.enabled and not stop.is_set():
            try:
                res = self._call("getUpdates", {"offset": self._offset, "timeout": 25,
                                                "allowed_updates": ["callback_query"]}, timeout=35)
            except (urllib.error.URLError, OSError):
                stop.wait(5)
                continue
            for upd in res.get("result", []):
                self._offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if not cq:
                    continue
                who = str(cq.get("from", {}).get("id"))
                chat = str(cq.get("message", {}).get("chat", {}).get("id"))
                reply = "Not allowed."
                parts = str(cq.get("data", "")).split(":")
                if who == self.chat_id and chat == self.chat_id and len(parts) == 3 and parts[0] == "gate":
                    status = {"approve": "approved", "reject": "rejected"}.get(parts[2])
                    if status and parts[1].isdigit():
                        reply = on_decision(int(parts[1]), status, f"telegram:{who}")
                try:
                    self._call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": reply[:190]})
                except (urllib.error.URLError, OSError):
                    pass
