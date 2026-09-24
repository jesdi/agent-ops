"""Durable per-issue operator message queue
(~/agent-ops-state/messages/<target>-<issue>.jsonl).

Keyed by (TARGET, ISSUE), not task — two projects can share an issue
number, and a reply must never cross into the other one's prompt. Not keyed
by task because an unclaimed backlog issue has no task-<N>.json,
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

from dispatcher.state import parse_task_key, task_key

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


def _path(state_dir: str | Path, target: str, issue: int) -> Path:
    return _dir(state_dir) / f"{task_key(target, issue)}.jsonl"


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


def append(state_dir: str | Path, target: str, issue: int, text: str,
           actor: str) -> Message:
    m = Message(id=str(uuid.uuid4()), text=text, actor=actor,
                created_at=_now())
    p = _path(state_dir, target, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(_dump(m) + "\n")
    return m


def all_messages(state_dir: str | Path, target: str, issue: int) -> list[Message]:
    p = _path(state_dir, target, issue)
    if not p.exists():
        return []
    out = [_parse(raw, p) for raw in p.read_text().splitlines() if raw.strip()]
    return [m for m in out if m is not None]


def undelivered(state_dir: str | Path, target: str, issue: int) -> list[Message]:
    return [m for m in all_messages(state_dir, target, issue) if not m.delivered_at]


def mark_delivered(state_dir: str | Path, target: str, issue: int,
                   ids: Sequence[str]) -> None:
    """Stamp delivered_at on the named ids. Already-stamped messages keep
    their original timestamp, so a re-drain never rewrites history."""
    p = _path(state_dir, target, issue)
    if not p.exists():
        return
    wanted = set(ids)
    stamp = _now()
    kept = [m if (m.id not in wanted or m.delivered_at)
            else Message(id=m.id, text=m.text, actor=m.actor,
                         created_at=m.created_at, delivered_at=stamp)
            for m in all_messages(state_dir, target, issue)]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(_dump(m) + "\n" for m in kept))
    tmp.replace(p)


def undelivered_counts(state_dir: str | Path) -> dict[tuple[str, int], int]:
    """(target, issue) -> number of queued (undelivered) messages. Used by
    the board badge; files not named `<target>-<issue>.jsonl` (including
    legacy issue-only ones) are ignored."""
    d = _dir(state_dir)
    if not d.exists():
        return {}
    out: dict[tuple[str, int], int] = {}
    for p in sorted(d.glob("*.jsonl")):
        key = parse_task_key(p.stem)
        if key is None:
            continue
        n = len(undelivered(state_dir, *key))
        if n:
            out[key] = n
    return out


def _merge_legacy(state_dir: str | Path, target: str, issue: int,
                  p: Path, dest: Path) -> None:
    """Fold legacy file `p` into an existing target-keyed `dest`. Atomic
    tmp+replace like mark_delivered; ids already present are skipped, so a
    crash before the unlink never duplicates on re-run."""
    existing = all_messages(state_dir, target, issue)
    seen = {m.id for m in existing}
    legacy = [m for m in (_parse(raw, p) for raw in
                          p.read_text().splitlines() if raw.strip())
              if m is not None and m.id not in seen]
    merged = sorted(existing + legacy, key=lambda m: m.created_at)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text("".join(_dump(m) + "\n" for m in merged))
    tmp.replace(dest)
    p.unlink()


def migrate_legacy(state_dir: str | Path, targets: Sequence[str]) -> None:
    """Take over pre-multi-project `<issue>.jsonl` files. With exactly one
    target they are renamed to that target's key — merged with an existing
    target-keyed file rather than clobbering it. With several they
    cannot be attributed, so they are left in place with a warning."""
    d = _dir(state_dir)
    if not d.exists():
        return
    for p in sorted(d.glob("*.jsonl")):
        if not p.stem.isdigit():
            continue
        if len(targets) != 1:
            print(f"[warn] legacy message file {p} not delivered: several "
                  f"targets are configured, so its project is unknown",
                  file=sys.stderr)
            continue
        issue = int(p.stem)
        dest = _path(state_dir, targets[0], issue)
        if not dest.exists():
            p.replace(dest)
            continue
        _merge_legacy(state_dir, targets[0], issue, p, dest)
