"""Append-only JSONL log of dispatcher stage transitions
(~/agent-ops-state/events.jsonl). The web console's history view and the
operator audit trail both read this file; the dispatcher appends at the
transition points it already executes and nowhere else. Appends must never
raise into a pass — a broken event log must not break dispatching."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EVENTS_FILE = "events.jsonl"
MAX_BYTES = 5 * 1024 * 1024          # rotate at 5 MB; archives are retained


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _events_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / EVENTS_FILE


def append_event(state_dir: str | Path, event: str, *, target: str = "",
                 issue: int = 0, stage: str = "", model: str = "",
                 actor: str = "dispatcher", detail: str = "") -> None:
    try:
        p = _events_path(state_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > MAX_BYTES:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            p.rename(p.with_name(f"events-{stamp}.jsonl"))
        line = json.dumps({"ts": _now(), "event": event, "target": target,
                           "issue": issue, "stage": stage, "model": model,
                           "actor": actor, "detail": detail})
        with p.open("a") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        print(f"[warn] event log append failed: {exc}", file=sys.stderr)


def read_tail(state_dir: str | Path, limit: int = 200) -> list[dict]:
    p = _events_path(state_dir)
    if not p.exists():
        return []
    events: list[dict] = []
    for raw in p.read_text().splitlines():
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            events.append(d)
    return events[-limit:]
