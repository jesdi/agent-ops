"""The task lifecycle (ADR 0014, specs/2026-11-10-task-lifecycle-state-machine).

A task is at a `stage` (pipeline position) with a `status` (what it does
there). Until the status is stored, `legacy_view` reads it from today's park
fields; the migration command applies the same function to every task file.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN,
                              PARK_WAKE, WHY_ACCESS, WHY_BAD_SIGNAL, WHY_CONFIG,
                              WHY_DECISION, WHY_LOOP_CAP, WHY_OPERATOR,
                              WHY_STALLED, Stage, TaskState, implement_done)


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


@dataclass(frozen=True)
class Status:
    """A status kind and its detail; a field a kind does not use stays empty.
    `reason`: of parked-human (decision | stalled | loop-cap | bad-signal |
    config | operator) or failed (session-crash | task-crash | killed |
    pr-closed | artifact | unknown). `path`, `fingerprint`: the page of
    held-plan-approval, parked-review and parked-answers. `origin`, `cause`:
    the status a waking task comes from (None for launch and next-stage) and
    why it wakes."""
    kind: Kind
    reason: str = ""
    note: str = ""
    path: str = ""
    fingerprint: str = ""
    run_id: int = 0
    origin: Status | None = None
    cause: str = ""


def attention(kind: str) -> str:
    """The cause of a parked-pr task woken by its PR."""
    return f"attention({kind})"


_RETIRED_STAGES = {Stage.BLOCKED: Stage.IMPLEMENT, Stage.STALLED_ON_BUDGET: Stage.SPEC}
_ENDED = {Stage.FAILED: Kind.FAILED, Stage.DONE: Kind.DONE, Stage.CANCELED: Kind.CANCELED}
_HUMAN_REASONS = {WHY_DECISION: "decision", WHY_STALLED: "stalled",
                  WHY_LOOP_CAP: "loop-cap", WHY_BAD_SIGNAL: "bad-signal",
                  WHY_CONFIG: "config", WHY_OPERATOR: "operator", "": "operator"}
_REQUEST_PARKS = {"answers": Kind.PARKED_ANSWERS, "plan-approval": Kind.PARKED_REVIEW}


def legacy_view(t: TaskState) -> tuple[Stage, Status]:
    """(stage, status) of a task stored with today's park fields: the table of
    the scenario "migration command rewrites each legacy shape". Raises
    ValueError on a shape that table does not classify."""
    if t.stage in _ENDED:
        return _ended(t)
    if t.stage in _RETIRED_STAGES:
        return _retired(t)
    stage = Stage.PLAN if t.stage is Stage.AWAITING_PLAN_REVIEW else t.stage
    if t.park == PARK_WAKE:
        return stage, _woken(t)
    if t.park:
        return stage, _parked(t)
    return _live(t)


def _ended(t: TaskState) -> tuple[Stage, Status]:
    """A failed, canceled or done task, whatever park it still carries."""
    if t.stage is Stage.FAILED and t.crashed_stage:
        return Stage(t.crashed_stage), Status(Kind.FAILED, reason="session-crash")
    kind = _ENDED[t.stage]
    stage = Stage.PR_OPEN if t.pr_number else Stage.QUEUED
    return stage, Status(kind, reason="unknown" if kind is Kind.FAILED else "")


def _retired(t: TaskState) -> tuple[Stage, Status]:
    stage = Stage.PR_OPEN if t.pr_number else _RETIRED_STAGES[t.stage]
    note = f"migrated from legacy {t.stage.value} stage"
    return stage, Status(Kind.PARKED_HUMAN, reason="operator", note=note)


def _woken(t: TaskState) -> Status:
    """A queued wake keeps what it would be still parked: its origin."""
    if t.crashed_stage:
        return Status(Kind.WAKING, origin=Status(Kind.FAILED, reason="session-crash"),
                      cause="resume")
    origin = _parked(t)
    cause = "access" if origin.kind is Kind.PARKED_ACCESS else "reply"
    return Status(Kind.WAKING, origin=origin, cause=cause)


def _parked(t: TaskState) -> Status:
    if t.park == PARK_LOGIN:
        return Status(Kind.HELD_LOGIN, note=t.park_note)
    if t.park == PARK_CI:
        return _parked_ci(t)
    if t.operator_request is not None:
        return Status(_REQUEST_PARKS[t.operator_request.kind], note=t.park_note,
                      path=t.operator_request.path,
                      fingerprint=t.operator_request.fingerprint)
    if t.park_reason == WHY_ACCESS:
        return Status(Kind.PARKED_ACCESS, note=t.park_note)
    return _parked_human(t)


def _parked_ci(t: TaskState) -> Status:
    if t.ci_run_id <= 0:
        raise ValueError(f"task {t.repository}#{t.issue}: awaiting-ci with no run id")
    return Status(Kind.PARKED_CI, run_id=t.ci_run_id)


def _parked_human(t: TaskState) -> Status:
    if t.park not in (PARK_HUMAN, PARK_WAKE) or t.park_reason not in _HUMAN_REASONS:
        raise ValueError(f"task {t.repository}#{t.issue}: unclassified park "
                         f"{t.park!r} reason {t.park_reason!r}")
    return Status(Kind.PARKED_HUMAN, reason=_HUMAN_REASONS[t.park_reason],
                  note=t.park_note)


def _live(t: TaskState) -> tuple[Stage, Status]:
    """A task with no park field set."""
    if t.unlaunched:
        return Stage.QUEUED, Status(Kind.WAKING, cause="launch")
    if t.stage is Stage.IMPLEMENT and implement_done(t):
        return Stage.REVIEW, Status(Kind.WAKING, cause="next-stage")
    if t.stage is Stage.PR_OPEN:
        return Stage.PR_OPEN, _at_pr(t)
    if t.stage is Stage.AWAITING_PLAN_REVIEW:
        return Stage.PLAN, _at_gate(t)
    return t.stage, Status(Kind.RUNNING)


def _at_pr(t: TaskState) -> Status:
    if t.feedback_pending:
        return Status(Kind.WAKING, origin=Status(Kind.PARKED_PR),
                      cause=attention(t.attention or "feedback"))
    return Status(Kind.PARKED_PR)


def _at_gate(t: TaskState) -> Status:
    """A live plan session at the review gate, held on its request."""
    if t.operator_request is None or t.operator_request.kind != "plan-approval":
        raise ValueError(f"task {t.repository}#{t.issue}: at the plan gate with no request")
    return Status(Kind.HELD_PLAN_APPROVAL, path=t.operator_request.path,
                  fingerprint=t.operator_request.fingerprint)
