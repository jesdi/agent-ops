"""Operator intent files (~/agent-ops-state/intents/): the web console's
session-backed write path into the dispatcher. The web writes one file per
action; the dispatcher drains them in lexical order at the top of each
pass, applies each through the same code path as the Telegram equivalent,
then deletes the file — applied-then-deleted, at-most-once. Intents
survive a reboot deliberately."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

INTENTS_DIR = "intents"
ACTIONS = ("reply", "park", "kill", "retry", "resume", "cancel")


@dataclass(frozen=True)
class Intent:
    action: str
    issue: int
    payload: dict
    actor: str
    created_at: str                   # iso8601 UTC
    path: Path


def _dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / INTENTS_DIR


def write_intent(state_dir: str | Path, action: str, issue: int, payload: dict,
                 actor: str, epoch_ms: int) -> Path:
    if action not in ACTIONS:
        raise ValueError(f"unknown intent action: {action!r}")
    d = _dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{epoch_ms}-{issue}-{action}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "action": action,
        "issue": issue,
        "payload": payload,
        "actor": actor,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    tmp.replace(p)
    return p


def list_intents(state_dir: str | Path) -> list[Intent]:
    d = _dir(state_dir)
    if not d.exists():
        return []
    out: list[Intent] = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = json.loads(p.read_text())
            out.append(Intent(action=rec["action"], issue=int(rec["issue"]),
                              payload=dict(rec.get("payload") or {}),
                              actor=str(rec.get("actor", "")),
                              created_at=str(rec.get("created_at", "")),
                              path=p))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError,
                OSError) as exc:
            print(f"[warn] malformed intent {p.name} deleted: {exc}",
                  file=sys.stderr)
            p.unlink(missing_ok=True)
    return out


def delete_intent(intent: Intent) -> None:
    intent.path.unlink(missing_ok=True)
