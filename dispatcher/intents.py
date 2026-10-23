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

from dispatcher.state import task_key

INTENTS_DIR = "intents"
ACTIONS = ("reply", "park", "kill", "retry", "resume", "cancel", "answers", "spec-repo",
           "replace-pr", "drop-pr", "depend-pr")


@dataclass(frozen=True)
class Intent:
    action: str
    target: str                       # "" = legacy file, predates target-keying
    issue: int
    payload: dict
    actor: str
    created_at: str                   # iso8601 UTC
    path: Path
    repo: str = ""                    # "" = legacy file, repository not proven


def _dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / INTENTS_DIR


def write_intent(state_dir: str | Path, action: str, target: str, issue: int,
                 payload: dict, actor: str, epoch_ms: int, *,
                 repo: str = "") -> Path:
    if action not in ACTIONS:
        raise ValueError(f"unknown intent action: {action!r}")
    d = _dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{epoch_ms}-{task_key(target, issue, repo)}-{action}.json"
    tmp = p.with_suffix(".tmp")
    body = {
        "action": action,
        "target": target,
        "issue": issue,
        "payload": payload,
        "actor": actor,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if repo:   # an unqualified intent keeps its old body
        body["repo"] = repo
    tmp.write_text(json.dumps(body, indent=2))
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
            # Identity comes from the JSON, never the filename stem.
            out.append(Intent(action=rec["action"],
                              target=str(rec.get("target", "")),
                              issue=int(rec["issue"]),
                              payload=dict(rec.get("payload") or {}),
                              actor=str(rec.get("actor", "")),
                              created_at=str(rec.get("created_at", "")),
                              path=p,
                              repo=str(rec.get("repo", ""))))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError,
                OSError) as exc:
            print(f"[warn] malformed intent {p.name} deleted: {exc}",
                  file=sys.stderr)
            p.unlink(missing_ok=True)
    return out


def delete_intent(intent: Intent) -> None:
    intent.path.unlink(missing_ok=True)
