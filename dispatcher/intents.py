"""Intent queues: one JSON file per action, <epoch_ms>-<key...>-<action>.json
under state_dir/<dir>, read in lexical order, applied, then deleted —
applied-then-deleted, at-most-once. A file that doesn't parse is deleted
with a warning. Intents survive a reboot deliberately.

Two queues: the dispatcher's (~/agent-ops-state/intents/, the web console's
session-backed write path; the dispatcher drains them at the top of each
pass through the same code path as the Telegram equivalent) and the
onboarding's (dispatcher.onboard)."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Queue:
    dir: str                  # under state_dir
    actions: tuple[str, ...]
    key: tuple[str, ...]      # the JSON fields naming the subject, in filename order

    def write(self, state_dir: str | Path, action: str, key: tuple, payload: dict,
              actor: str, epoch_ms: int) -> Path:
        if action not in self.actions:
            raise ValueError(f"unknown intent action: {action!r}")
        d = Path(state_dir) / self.dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{'-'.join(map(str, (epoch_ms, *key, action)))}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "action": action,
            **dict(zip(self.key, key, strict=True)),
            "payload": payload,
            "actor": actor,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2))
        tmp.replace(p)
        return p

    def read(self, state_dir: str | Path, parse: Callable[[dict, Path], T]) -> list[T]:
        """Every intent, `parse`d from its JSON and path, in lexical order.
        `parse` raises KeyError, TypeError or ValueError on a malformed one."""
        d = Path(state_dir) / self.dir
        if not d.exists():
            return []
        out: list[T] = []
        for p in sorted(d.glob("*.json")):
            try:
                out.append(parse(json.loads(p.read_text()), p))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError,
                    OSError) as exc:
                print(f"[warn] malformed intent {p.name} deleted: {exc}",
                      file=sys.stderr)
                p.unlink(missing_ok=True)
        return out


ACTIONS = ("reply", "park", "kill", "retry", "resume", "cancel", "answers")
DISPATCHER = Queue("intents", ACTIONS, ("repository", "issue"))


@dataclass(frozen=True)
class Intent:
    action: str
    repository: str                       # "" = legacy file, predates repository-keying
    issue: int
    payload: dict
    actor: str
    created_at: str                   # iso8601 UTC
    path: Path


def write_intent(state_dir: str | Path, action: str, repository: str, issue: int,
                 payload: dict, actor: str, epoch_ms: int) -> Path:
    return DISPATCHER.write(state_dir, action, (repository, issue), payload, actor,
                            epoch_ms)


def _intent(rec: dict, p: Path) -> Intent:
    # Identity comes from the JSON, never the filename stem.
    return Intent(action=rec["action"],
                  repository=str(rec.get("repository", rec.get("target", ""))),
                  issue=int(rec["issue"]),
                  payload=dict(rec.get("payload") or {}),
                  actor=str(rec.get("actor", "")),
                  created_at=str(rec.get("created_at", "")),
                  path=p)


def list_intents(state_dir: str | Path) -> list[Intent]:
    return DISPATCHER.read(state_dir, _intent)


def delete_intent(intent: Intent) -> None:
    intent.path.unlink(missing_ok=True)
