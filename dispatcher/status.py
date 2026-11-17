"""What a task does at its stage (ADR 0014): a status kind and its detail,
as the task file stores it (design.md, Data model)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    """The 13 leaf status kinds the table is total over."""
    RUNNING = "running"
    HELD_PLAN_APPROVAL = "held-plan-approval"
    HELD_LOGIN = "held-login"
    PARKED_HUMAN = "parked-human"
    PARKED_ANSWERS = "parked-answers"
    PARKED_ACCESS = "parked-access"
    PARKED_CI = "parked-ci"
    PARKED_REVIEW = "parked-review"
    PARKED_PR = "parked-pr"
    WAKING = "waking"
    FAILED = "failed"
    CANCELED = "canceled"
    DONE = "done"


class HumanReason(str, Enum):
    """Why a task is parked for the operator (requirement 1)."""
    DECISION = "decision"
    STALLED = "stalled"
    LOOP_CAP = "loop-cap"
    BAD_SIGNAL = "bad-signal"
    CONFIG = "config"
    OPERATOR = "operator"


class FailReason(str, Enum):
    """Why a task failed (requirement 10 guards on it)."""
    SESSION_CRASH = "session-crash"
    TASK_CRASH = "task-crash"
    KILLED = "killed"
    PR_CLOSED = "pr-closed"
    ARTIFACT = "artifact"
    UNKNOWN = "unknown"


class Cause(str, Enum):
    """Why a task wakes (requirement 11); attention(kind) is one value per
    kind of PR attention."""
    REPLY = "reply"
    RESUME = "resume"
    RANKED = "ranked"
    CI = "ci"
    ACCESS = "access"
    LAUNCH = "launch"
    NEXT_STAGE = "next-stage"
    ATTENTION_FEEDBACK = "attention(feedback)"
    ATTENTION_CHECK_FAILED = "attention(check-failed)"
    ATTENTION_CONFLICT = "attention(conflict)"
    ATTENTION_OPERATOR = "attention(operator)"


@dataclass(frozen=True)
class Status:
    """A status kind and its detail; a field a kind does not use stays empty.
    `reason`: a HumanReason of parked-human, a FailReason of failed. `path`,
    `fingerprint`: the page of held-plan-approval, parked-review and
    parked-answers. `origin`, `cause`: the status a waking task comes from
    (None for launch and next-stage) and why it wakes."""
    kind: Kind
    reason: HumanReason | FailReason | None = None
    note: str = ""
    path: str = ""
    fingerprint: str = ""
    run_id: int = 0
    origin: Status | None = None
    cause: Cause | None = None


RUNNING = Status(Kind.RUNNING)

_OCCUPYING = frozenset({Kind.RUNNING, Kind.HELD_PLAN_APPROVAL, Kind.HELD_LOGIN})
_ENDED = frozenset({Kind.FAILED, Kind.CANCELED, Kind.DONE})
_CRASHES = frozenset({FailReason.SESSION_CRASH, FailReason.TASK_CRASH})


def occupies(status: Status) -> bool:
    """Does a task in `status` hold a slot and a capacity unit?"""
    return status.kind in _OCCUPYING


def ended(status: Status) -> bool:
    """Failed, canceled or done: the task keeps its stage and is never
    driven again, bar a Resume."""
    return status.kind in _ENDED


def crashed(status: Status) -> bool:
    """Failed by a crash: Resume starts its stage afresh."""
    return status.kind is Kind.FAILED and status.reason in _CRASHES


# ---- The stored shape: {kind, detail} -------------------------------------

_PAGE = ("page", "fingerprint", "note")
# The detail keys each kind stores; a key missing on load reads as empty.
_DETAIL: dict[Kind, tuple[str, ...]] = {
    Kind.RUNNING: (), Kind.CANCELED: (), Kind.DONE: (), Kind.PARKED_PR: (),
    Kind.HELD_PLAN_APPROVAL: _PAGE, Kind.PARKED_REVIEW: _PAGE,
    Kind.PARKED_ANSWERS: ("path", "fingerprint", "note"),
    Kind.PARKED_HUMAN: ("reason", "note"),
    Kind.PARKED_CI: ("run_id",),
    Kind.PARKED_ACCESS: ("note",), Kind.HELD_LOGIN: ("note",),
    Kind.WAKING: ("from", "cause"),
    Kind.FAILED: ("reason",),
}
_FIELD = {"page": "path", "from": "origin"}   # the stored key -> the Status field
_REASONS = {Kind.PARKED_HUMAN: HumanReason, Kind.FAILED: FailReason}


def to_json(status: Status) -> dict:
    detail = {key: _stored(getattr(status, _FIELD.get(key, key)))
              for key in _DETAIL[status.kind]}
    return {"kind": status.kind.value, "detail": detail}


def _stored(value: object) -> object:
    if isinstance(value, Status):
        return to_json(value)
    return value.value if isinstance(value, Enum) else value


def from_json(raw: object) -> Status:
    """The stored status; ValueError for one that is not a status."""
    if not isinstance(raw, dict) or not isinstance(raw.get("detail", {}), dict):
        raise ValueError(f"status {raw!r} is not {{kind, detail}}")
    kind = Kind(raw.get("kind"))
    detail = raw.get("detail", {})
    fields = {_FIELD.get(key, key): _loaded(kind, key, detail[key])
              for key in _DETAIL[kind] if detail.get(key) is not None}
    return Status(kind, **fields)


def _loaded(kind: Kind, key: str, value: object) -> object:
    if key == "from":
        return from_json(value)
    if key == "cause":
        return Cause(value)
    if key == "reason":
        return _REASONS[kind](value)
    return value


# ---- What a save refuses ----------------------------------------------------

def problem(stage: str, status: Status) -> str:
    """Why `status` at `stage` breaks a constraint of design.md's Data model:
    a broken detail, or a kind or a wake at a stage it is never at. A waking
    task's origin is checked at its stage too. "" when it keeps them all."""
    return _detail_problem(status) or _stage_problem(stage, status)


def _detail_problem(status: Status) -> str:
    return (_reason_problem(status) or _page_problem(status)
            or _run_problem(status)
            or (_waking_problem(status) if status.kind is Kind.WAKING else ""))


# The stages (by value: Stage lives in state.py, which imports this module) a
# status kind, or a waking cause, can be at; any stage when not named.
_KIND_STAGES = {Kind.HELD_PLAN_APPROVAL: {"plan"}, Kind.PARKED_REVIEW: {"plan"},
                Kind.PARKED_PR: {"pr-open"}}
_CAUSE_STAGES = {Cause.LAUNCH: {"queued"}, Cause.NEXT_STAGE: {"review"}}


def _stage_problem(stage: str, status: Status) -> str:
    if stage not in _KIND_STAGES.get(status.kind, (stage,)):
        return status.kind.value
    if stage not in _CAUSE_STAGES.get(status.cause, (stage,)):
        return f"waking({status.cause.value})"
    return _stage_problem(stage, status.origin) if status.origin else ""


def _reason_problem(status: Status) -> str:
    kind = status.kind
    if kind in _REASONS and not isinstance(status.reason, _REASONS[kind]):
        return f"{kind.value} with reason {status.reason!r}"
    return ""


def _page_problem(status: Status) -> str:
    # The fingerprint may be empty on purpose: a rework session that dies at the
    # gate with no rounds left parks parked(review(page, "")), so no approve.
    kind = status.kind
    if kind in (Kind.HELD_PLAN_APPROVAL, Kind.PARKED_REVIEW, Kind.PARKED_ANSWERS) \
            and not status.path:
        return f"{kind.value} with no page"
    return ""


def _run_problem(status: Status) -> str:
    if status.kind is Kind.PARKED_CI and status.run_id <= 0:
        return f"parked-ci for run {status.run_id}"
    return ""


# A waking task's launch reads its origin; these it can never come from.
_NO_ORIGIN = frozenset({Kind.WAKING, Kind.CANCELED, Kind.DONE})
_FROM_NOTHING = frozenset({Cause.LAUNCH, Cause.NEXT_STAGE})


def _waking_problem(status: Status) -> str:
    if status.cause is None:
        return "waking with no cause"
    if (status.origin is None) != (status.cause in _FROM_NOTHING):
        return f"waking({status.cause.value}) from {status.origin!r}"
    if status.origin is not None and status.origin.kind in _NO_ORIGIN:
        return f"waking from {status.origin.kind.value}"
    return _detail_problem(status.origin) if status.origin is not None else ""
