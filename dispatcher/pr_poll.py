"""Pure classification of a `gh pr view` payload against a task's feedback
cursor. No I/O — the dispatcher pass fetches, this module only decides:
merged | closed | feedback | check-failed | conflict | quiet.

`reviewDecision` is deliberately ignored as a trigger: CHANGES_REQUESTED
latches until re-review, so acting on it would re-open an already-addressed
round forever. Only timestamped events (reviews, comments) newer than the
cursor trigger, and only from humans other than the box itself."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class CIStatus:
    """Latest result of a workflow or commit-status context on the PR head."""
    conclusion: str
    completed_at: str


@dataclass(frozen=True)
class PollResult:
    kind: str            # merged | closed | feedback | check-failed | conflict | quiet
    latest_ts: str = ""  # feedback: newest human ts; check-failed: newest red completion; conflict: head sha


_RED = {"FAILURE", "ERROR", "TIMED_OUT"}
_GREEN = {"SUCCESS", "SKIPPED", "NEUTRAL"}


def ci_verdict(conclusion: str) -> str:
    """"red", "green" or "pending" for the conclusion of a check run or
    workflow run, or the state of a commit status; "" (unfinished),
    "pending" and the rest (cancelled, action_required, …) are pending."""
    c = conclusion.upper()
    return "red" if c in _RED else "green" if c in _GREEN else "pending"


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
             check_cursor: str = "", conflict_cursor: str = "",
             ci_statuses: Sequence[CIStatus] = ()) -> PollResult:
    if payload.get("mergedAt"):
        return PollResult("merged")
    if payload.get("state") == "CLOSED":
        return PollResult("closed")
    if fresh := _newest_feedback(payload, cursor, self_login):
        return PollResult("feedback", latest_ts=fresh)
    if red := _newest_red(ci_statuses, check_cursor):
        return PollResult("check-failed", latest_ts=red)
    head = str(payload.get("headRefOid") or "")
    if payload.get("mergeable") == "CONFLICTING" and head and head != conflict_cursor:
        return PollResult("conflict", latest_ts=head)
    return PollResult("quiet")


def _newest_feedback(payload: dict, cursor: str, self_login: str) -> str:
    """The raw timestamp of the newest human review or comment after
    `cursor`; "" when there is none."""
    since = _ts(cursor)
    fresh: list[tuple[datetime, str]] = []
    for raw, author in _events(payload):
        dt = _ts(raw)
        if dt is None or not _is_human(author, self_login):
            continue
        if since is None or dt > since:
            fresh.append((dt, raw))
    return max(fresh)[1] if fresh else ""


def _events(payload: dict) -> list[tuple[str, dict | None]]:
    """(timestamp, author) of every review and comment."""
    return ([(r.get("submittedAt") or "", r.get("author"))
             for r in payload.get("reviews") or []]
            + [(c.get("createdAt") or "", c.get("author"))
               for c in payload.get("comments") or []])


def _newest_red(ci_statuses: Sequence[CIStatus], check_cursor: str) -> str:
    """The raw completion time of the newest red check after
    `check_cursor`; "" when there is none."""
    red: list[tuple[datetime, str]] = []
    seen = _ts(check_cursor)
    for check in ci_statuses:
        raw = check.completed_at if ci_verdict(check.conclusion) == "red" else ""
        dt = _ts(raw)
        if dt is not None and (seen is None or dt > seen):
            red.append((dt, raw))
    return max(red)[1] if red else ""
