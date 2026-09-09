"""Pure classification of a `gh pr view` payload against a task's feedback
cursor. No I/O — the dispatcher pass fetches, this module only decides:
merged | closed | feedback | check-failed | conflict | quiet.

`reviewDecision` is deliberately ignored as a trigger: CHANGES_REQUESTED
latches until re-review, so acting on it would re-open an already-addressed
round forever. Only timestamped events (reviews, comments) newer than the
cursor trigger, and only from humans other than the box itself."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class PollResult:
    kind: str            # merged | closed | feedback | check-failed | conflict | quiet
    latest_ts: str = ""  # feedback: newest human ts; check-failed: newest red completion; conflict: head sha


_RED = {"FAILURE", "ERROR", "TIMED_OUT"}


def _red_at(check: dict) -> str:
    """Completion timestamp of a failed check (CheckRun or StatusContext),
    "" when it is not red or carries no timestamp — a red check with no
    time cannot be cursored, so it never triggers."""
    verdict = (check.get("conclusion") or check.get("state") or "").upper()
    if verdict not in _RED:
        return ""
    return check.get("completedAt") or check.get("startedAt") or check.get("createdAt") or ""


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


def classify(payload: dict, cursor: str, self_login: str,
             check_cursor: str = "", conflict_cursor: str = "") -> PollResult:
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
    red: list[tuple[datetime, str]] = []
    seen = _ts(check_cursor)
    for check in payload.get("statusCheckRollup") or []:
        raw = _red_at(check if isinstance(check, dict) else {})
        dt = _ts(raw)
        if dt is not None and (seen is None or dt > seen):
            red.append((dt, raw))
    if red:
        return PollResult("check-failed", latest_ts=max(red)[1])
    head = str(payload.get("headRefOid") or "")
    if payload.get("mergeable") == "CONFLICTING" and head and head != conflict_cursor:
        return PollResult("conflict", latest_ts=head)
    return PollResult("quiet")
