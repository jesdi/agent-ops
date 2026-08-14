"""Durable per-issue operator message queue
(~/agent-ops-state/messages/<issue>.jsonl).

Keyed by ISSUE, not task: an unclaimed backlog issue has no task-<N>.json,
and the queue must survive done/failed/retry cycles that delete or rewrite
one. Appended by the dispatcher only (the web writes intents; the dispatcher
stays the single writer of state) and drained at session boundaries.

Normal operation is append-only; stamping delivery is the one rewrite, done
atomically via tmp+replace. A malformed line is skipped with a warning and
dropped on the next rewrite — a corrupt queue must never break a pass, the
same contract as intents.list_intents and eventlog.append_event."""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

MESSAGES_DIR = "messages"


@dataclass(frozen=True)
class Message:
    id: str
    text: str
    actor: str
    created_at: str          # iso8601 UTC
    delivered_at: str = ""   # "" = still queued


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / MESSAGES_DIR


def _path(state_dir: str | Path, issue: int) -> Path:
    return _dir(state_dir) / f"{issue}.jsonl"


def _parse(raw: str, where: Path) -> Message | None:
    try:
        d = json.loads(raw)
        return Message(id=str(d["id"]), text=str(d["text"]),
                       actor=str(d.get("actor", "")),
                       created_at=str(d.get("created_at", "")),
                       delivered_at=str(d.get("delivered_at") or ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"[warn] malformed message line in {where.name} skipped: {exc}",
              file=sys.stderr)
        return None


def _dump(m: Message) -> str:
    return json.dumps({"id": m.id, "text": m.text, "actor": m.actor,
                       "created_at": m.created_at,
                       "delivered_at": m.delivered_at or None})


def append(state_dir: str | Path, issue: int, text: str,
           actor: str) -> Message:
    m = Message(id=str(uuid.uuid4()), text=text, actor=actor,
                created_at=_now())
    p = _path(state_dir, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(_dump(m) + "\n")
    return m


def all_messages(state_dir: str | Path, issue: int) -> list[Message]:
    p = _path(state_dir, issue)
    if not p.exists():
        return []
    out = [_parse(raw, p) for raw in p.read_text().splitlines() if raw.strip()]
    return [m for m in out if m is not None]


def undelivered(state_dir: str | Path, issue: int) -> list[Message]:
    return [m for m in all_messages(state_dir, issue) if not m.delivered_at]


def mark_delivered(state_dir: str | Path, issue: int,
                   ids: Sequence[str]) -> None:
    """Stamp delivered_at on the named ids. Already-stamped messages keep
    their original timestamp, so a re-drain never rewrites history."""
    p = _path(state_dir, issue)
    if not p.exists():
        return
    wanted = set(ids)
    stamp = _now()
    kept = [m if (m.id not in wanted or m.delivered_at)
            else Message(id=m.id, text=m.text, actor=m.actor,
                         created_at=m.created_at, delivered_at=stamp)
            for m in all_messages(state_dir, issue)]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(_dump(m) + "\n" for m in kept))
    tmp.replace(p)


def undelivered_counts(state_dir: str | Path) -> dict[int, int]:
    """issue -> number of queued (undelivered) messages. Used by the board
    badge; files whose stem is not an issue number are ignored."""
    d = _dir(state_dir)
    if not d.exists():
        return {}
    out: dict[int, int] = {}
    for p in sorted(d.glob("*.jsonl")):
        try:
            issue = int(p.stem)
        except ValueError:
            continue
        n = len(undelivered(state_dir, issue))
        if n:
            out[issue] = n
    return out
