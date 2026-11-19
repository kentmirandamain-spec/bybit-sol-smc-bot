"""Telegram alerts and commands. Stdlib only. Ignores every chat except TELEGRAM_CHAT_ID."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path


def credentials(root: Path | None = None) -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if (not token or not chat) and root is not None:
        env = Path(root) / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if key.strip() == "TELEGRAM_BOT_TOKEN" and not token:
                    token = value
                if key.strip() == "TELEGRAM_CHAT_ID" and not chat:
                    chat = value
    return token, chat


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.offset = 0
        self.close_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:4000]})
        return True

    def poll(self, handler, timeout: int = 0) -> None:
        if not self.enabled:
            return
        data = self._call("getUpdates", {"offset": self.offset, "timeout": timeout})
        for update in data.get("result") or []:
            self.offset = int(update.get("update_id") or 0) + 1
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id", ""))
            if chat != self.chat_id:
                continue
            reply = handler(str(message.get("text") or ""))
            if reply:
                self.send(reply)

    def _call(self, method: str, payload: dict) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_for(payload)) as resp:
            return json.loads(resp.read().decode())


def timeout_for(payload: dict) -> int:
    return int(payload.get("timeout") or 0) + 15


def handle_text(text: str, close_until: float, now: float | None = None) -> tuple[str, float, str]:
    """Return (reply, new close deadline, action). action is '' or pause/resume/close."""
    now = time.time() if now is None else now
    cmd = text.strip().split()
    if not cmd:
        return "", close_until, ""
    name = cmd[0].split("@")[0].lower()
    if name == "/close":
        if len(cmd) > 1 and cmd[1].lower() == "confirm" and now <= close_until:
            return "Closing the SOL position.", 0.0, "close"
        return "Send /close confirm within 60 seconds to close the position.", now + 60, ""
    if name == "/pause":
        return "New entries paused.", close_until, "pause"
    if name == "/resume":
        return "New entries resumed.", close_until, "resume"
    if name == "/status":
        return "", close_until, "status"
    if name == "/trades":
        return "", close_until, "trades"
    if name == "/pnl":
        return "", close_until, "pnl"
    if name == "/help":
        return ("Commands: /status /pause /resume /close (then /close confirm within 60s) "
                "/trades /pnl /help"), close_until, ""
    return "", close_until, ""
