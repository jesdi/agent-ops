"""Pure classification of a `gh pr view` payload against a task's feedback
cursor. No I/O — the dispatcher pass fetches, this module only decides:
merged | closed | feedback | quiet.

`reviewDecision` is deliberately ignored as a trigger: CHANGES_REQUESTED
latches until re-review, so acting on it would re-open an already-addressed
round forever. Only timestamped events (reviews, comments) newer than the
cursor trigger, and only from humans other than the box itself."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class PollResult:
    kind: str            # "merged" | "closed" | "feedback" | "quiet"
    latest_ts: str = ""  # newest human feedback timestamp (kind=="feedback")


def _ts(raw: str) -> datetime | None:
    """GitHub emits `…Z`, the dispatcher's _now() emits `…+00:00` — parse
    both; unparseable reads as absent (never a false trigger)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_human(author: dict | None, self_login: str) -> bool:
    a = author or {}
    login = a.get("login") or ""
    return bool(login) and login != self_login \
        and not login.endswith("[bot]") and not a.get("is_bot", False)


def classify(payload: dict, cursor: str, self_login: str) -> PollResult:
    if payload.get("mergedAt"):
        return PollResult("merged")
    if payload.get("state") == "CLOSED":
        return PollResult("closed")
    since = _ts(cursor)
    fresh: list[tuple[datetime, str]] = []
    events = ([(r.get("submittedAt") or "", r.get("author"))
               for r in payload.get("reviews") or []]
              + [(c.get("createdAt") or "", c.get("author"))
                 for c in payload.get("comments") or []])
    for raw, author in events:
        dt = _ts(raw)
        if dt is None or not _is_human(author, self_login):
            continue
        if since is None or dt > since:
            fresh.append((dt, raw))
    if fresh:
        return PollResult("feedback", latest_ts=max(fresh)[1])
    return PollResult("quiet")
