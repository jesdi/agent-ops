"""Inbound side of the Telegram bot: short-poll getUpdates once per
dispatcher pass. Single private chat; the park-notification message is the
session handle (reply-to-message-id → task). Offset advances immediately
after fetch: at-most-once delivery — a crashed pass drops updates rather
than double-injecting text into a session."""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Reply:
    reply_to_msg_id: int
    text: str


@dataclass(frozen=True)
class Command:
    name: str  # "status" | "attach" | "queue" | "boost" | "next"
    issue: int = 0
    repo: str = ""    # "owner/name" of a qualified `/attach|/boost|/next owner/name#12`
    amount: int = 0   # /boost +k, /demote -k
    force: bool = False


@dataclass(frozen=True)
class Plain:
    text: str


_ATTACH_REF = re.compile(r"(?:(?P<repo>[\w.-]+/[\w.-]+)#)?(?P<issue>\d+)")


def _http_get(url: str) -> dict:
    return json.loads(urllib.request.urlopen(url, timeout=15).read() or b"{}")


def _step(token: str) -> int:
    """The positive integer of a /boost or /demote step, 0 when it is not one."""
    return int(token) if token.isdecimal() and int(token) >= 1 else 0


def _ref(token: str):
    return _ATTACH_REF.fullmatch(token)


def _boost(parts: list[str]) -> Command | None:
    ref = _ref(parts[1]) if len(parts) in (2, 3) else None
    step = _step(parts[2]) if len(parts) == 3 else 1
    if ref is None or not step:
        return None
    sign = 1 if parts[0] == "/boost" else -1
    return Command(name="boost", issue=int(ref["issue"]), repo=ref["repo"] or "",
                   amount=sign * step)


def _next(parts: list[str]) -> Command | None:
    ref = _ref(parts[1]) if len(parts) in (2, 3) else None
    if ref is None or (len(parts) == 3 and parts[2] != "force"):
        return None
    return Command(name="next", issue=int(ref["issue"]), repo=ref["repo"] or "",
                   force=len(parts) == 3)


def _attach(parts: list[str]) -> Command | None:
    ref = _ref(parts[1]) if len(parts) == 2 else None
    if ref is None:
        return None
    return Command(name="attach", issue=int(ref["issue"]), repo=ref["repo"] or "")


def _command(parts: list[str]) -> Command | None:
    name = parts[0]
    if name == "/status":
        return Command(name="status")
    if name == "/queue" and len(parts) == 1:
        return Command(name="queue")
    handler = {"/attach": _attach, "/boost": _boost, "/demote": _boost,
               "/next": _next}.get(name)
    return handler(parts) if handler else None


def classify(text: str, reply_to_msg_id: int):
    text = text.strip()
    if not text:
        return None
    if reply_to_msg_id:
        return Reply(reply_to_msg_id=reply_to_msg_id, text=text)
    if text.startswith("/"):
        return _command(text.split())
    return Plain(text=text)


def fetch_events(state_dir: str | Path) -> list:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return []
    offset_file = Path(state_dir) / "telegram-offset"
    offset = int(offset_file.read_text()) if offset_file.exists() else 0
    try:
        resp = _http_get(f"https://api.telegram.org/bot{token}/getUpdates"
                         f"?offset={offset}&timeout=0")
    except (OSError, http.client.HTTPException, ValueError) as e:
        print(f"telegram getUpdates failed: {e}", file=sys.stderr)
        return []
    updates = resp.get("result") or []
    if updates:
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        offset_file.write_text(str(max(u["update_id"] for u in updates) + 1))
    events = []
    for u in updates:
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        ev = classify(msg.get("text") or "",
                      (msg.get("reply_to_message") or {}).get("message_id") or 0)
        if ev is not None:
            events.append(ev)
    return events
