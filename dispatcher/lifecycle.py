"""The task lifecycle (ADR 0014, specs/2026-11-10-task-lifecycle-state-machine).

A task is at a `stage` (pipeline position) with a `status` (what it does
there). Until the status is stored, `legacy_view` reads it from today's park
fields; the migration command applies the same function to every task file.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, NamedTuple

from dispatcher.machine import Notify
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN,
                              PARK_WAKE, WHY_ACCESS, WHY_BAD_SIGNAL, WHY_CONFIG,
                              WHY_DECISION, WHY_LOOP_CAP, WHY_OPERATOR,
                              WHY_STALLED, AnswersRequest, OperatorRequest,
                              PlanApprovalRequest, Stage, TaskState,
                              implement_done)


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


class At(NamedTuple):
    """Where a task is in its lifecycle."""
    stage: Stage
    status: Status


def _at(task: TaskState | At) -> At:
    """`task` as (stage, status), never its raw stage beside its status."""
    return task if isinstance(task, At) else task.at


_OCCUPYING = frozenset({Kind.RUNNING, Kind.HELD_PLAN_APPROVAL, Kind.HELD_LOGIN})


def occupies(status: Status) -> bool:
    """Does a task in `status` hold a slot and a capacity unit?"""
    return status.kind in _OCCUPYING


def operator_request(task: TaskState | At) -> OperatorRequest | None:
    """What the operator is asked for, derived from the status."""
    return _request(_at(task).status)


def _request(status: Status) -> OperatorRequest | None:
    if status.kind is Kind.WAKING:
        return _request(status.origin) if status.origin else None
    if status.kind in (Kind.HELD_PLAN_APPROVAL, Kind.PARKED_REVIEW):
        return PlanApprovalRequest(path=status.path, fingerprint=status.fingerprint)
    if status.kind is Kind.PARKED_ANSWERS:
        return AnswersRequest(path=status.path, fingerprint=status.fingerprint)
    return None


_RETIRED_STAGES = {Stage.BLOCKED: Stage.IMPLEMENT, Stage.STALLED_ON_BUDGET: Stage.SPEC}
_ENDED = {Stage.FAILED: Kind.FAILED, Stage.DONE: Kind.DONE, Stage.CANCELED: Kind.CANCELED}
# today's park_reason spellings
_HUMAN_REASONS = {WHY_DECISION: HumanReason.DECISION, WHY_STALLED: HumanReason.STALLED,
                  WHY_LOOP_CAP: HumanReason.LOOP_CAP, WHY_BAD_SIGNAL: HumanReason.BAD_SIGNAL,
                  WHY_CONFIG: HumanReason.CONFIG, WHY_OPERATOR: HumanReason.OPERATOR,
                  "": HumanReason.OPERATOR}
_REQUEST_PARKS = {"answers": Kind.PARKED_ANSWERS, "plan-approval": Kind.PARKED_REVIEW}


def legacy_view(t: TaskState) -> At:
    """(stage, status) of a task stored with today's park fields: the table of
    the scenario "migration command rewrites each legacy shape". Raises
    ValueError on a shape that table does not classify."""
    if t.stage in _ENDED:
        return At(*_ended(t))
    if t.stage in _RETIRED_STAGES:
        return At(*_retired(t))
    stage = Stage.PLAN if t.stage is Stage.AWAITING_PLAN_REVIEW else t.stage
    if t.park == PARK_WAKE:
        return At(stage, _woken(t))
    if t.park:
        return At(stage, _parked(t))
    return At(*_live(t))


def _ended(t: TaskState) -> tuple[Stage, Status]:
    """A failed, canceled or done task, whatever park it still carries."""
    if t.stage is Stage.FAILED and t.crashed_stage:
        return Stage(t.crashed_stage), Status(Kind.FAILED, reason=FailReason.SESSION_CRASH)
    kind = _ENDED[t.stage]
    stage = Stage.PR_OPEN if t.pr_number else Stage.QUEUED
    return stage, Status(kind, reason=FailReason.UNKNOWN if kind is Kind.FAILED else None)


def _retired(t: TaskState) -> tuple[Stage, Status]:
    stage = Stage.PR_OPEN if t.pr_number else _RETIRED_STAGES[t.stage]
    note = f"migrated from legacy {t.stage.value} stage"
    return stage, Status(Kind.PARKED_HUMAN, reason=HumanReason.OPERATOR, note=note)


def _woken(t: TaskState) -> Status:
    """A queued wake keeps what it would be still parked: its origin."""
    if t.crashed_stage:
        return Status(Kind.WAKING, origin=Status(Kind.FAILED, reason=FailReason.SESSION_CRASH),
                      cause=Cause.RESUME)
    origin = _parked(t)
    cause = Cause.ACCESS if origin.kind is Kind.PARKED_ACCESS else Cause.REPLY
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
        return Stage.QUEUED, Status(Kind.WAKING, cause=Cause.LAUNCH)
    if t.stage is Stage.IMPLEMENT and implement_done(t):
        return Stage.REVIEW, Status(Kind.WAKING, cause=Cause.NEXT_STAGE)
    if t.stage is Stage.PR_OPEN:
        return Stage.PR_OPEN, _at_pr(t)
    if t.stage is Stage.AWAITING_PLAN_REVIEW:
        return Stage.PLAN, _at_gate(t)
    return t.stage, Status(Kind.RUNNING)


def _at_pr(t: TaskState) -> Status:
    if t.feedback_pending:
        return Status(Kind.WAKING, origin=Status(Kind.PARKED_PR),
                      cause=Cause(f"attention({t.attention or 'feedback'})"))
    return Status(Kind.PARKED_PR)


def _at_gate(t: TaskState) -> Status:
    """A live plan session at the review gate, held on its request."""
    if t.operator_request is None or t.operator_request.kind != "plan-approval":
        raise ValueError(f"task {t.repository}#{t.issue}: at the plan gate with no request")
    return Status(Kind.HELD_PLAN_APPROVAL, path=t.operator_request.path,
                  fingerprint=t.operator_request.fingerprint)



# ---- Events ---------------------------------------------------------------

class EventType(str, Enum):
    """What can happen to a task (requirement 6). Session events come with
    the session-signal path."""
    OP_RESUME = "op-resume"
    OP_REPLY = "op-reply"
    OP_ANSWERS = "op-answers"
    OP_KILL = "op-kill"
    OP_CANCEL = "op-cancel"
    OP_PARK = "op-park"
    OP_LOGIN_CODE = "op-login-code"
    RESUME_ADMITTED = "resume-admitted"
    CANDIDATE_RANKED = "candidate-ranked"
    FLUSH_DUE = "flush-due"
    CI_FINISHED = "ci-finished"
    ACCESS_REACHED = "access-reached"
    PR_FEEDBACK = "pr-feedback"
    PR_CHECK_FAILED = "pr-check-failed"
    PR_CONFLICT = "pr-conflict"
    PR_MERGED = "pr-merged"
    PR_CLOSED = "pr-closed"


@dataclass(frozen=True)
class Event:
    """An event and the facts the executor read for it (decide reads no file,
    clock or session). Every default is the plain case."""
    type: EventType
    blocked: str = ""              # resume-admitted: why the launch cannot start now
    exhausted: bool = False        # ci-finished, pr-check-failed, pr-conflict: no ci round left
    this_pass: bool = False        # candidate-ranked: the task ended in this pass
    left_login_prompt: bool = False  # op-login-code: the pane shows no login prompt any more
    model: str = ""                # op-resume: the model override
    bypass_usage: bool = False     # op-resume: launch past the usage gate
    code: str = ""                 # op-login-code: never logged


# ---- Outcomes and actions ---------------------------------------------------

@dataclass(frozen=True)
class Transition:
    status: Status
    stage: Stage
    actions: tuple = ()


@dataclass(frozen=True)
class Ignore:
    reason: str


@dataclass(frozen=True)
class Reject:
    reason: str


@dataclass(frozen=True)
class AcquireSlot:
    """The task starts occupying: it takes a slot and a capacity unit."""


@dataclass(frozen=True)
class ReleaseSlot:
    """The task stops occupying: slot = NO_SLOT."""


@dataclass(frozen=True)
class EndSession:
    pass


@dataclass(frozen=True)
class ContinueSession:
    """Resume the stage's recorded session with the queued messages."""


@dataclass(frozen=True)
class RespawnStage:
    """Start `stage` afresh in the same worktree, never --continue."""
    stage: Stage


@dataclass(frozen=True)
class SpawnAddressReview:
    pass


@dataclass(frozen=True)
class WakeBlocked:
    """The admitted wake has no capacity or slot (edge-triggered)."""
    reason: str


@dataclass(frozen=True)
class SetResumeOverride:
    model: str
    bypass_usage: bool


@dataclass(frozen=True)
class ResetRounds:
    """An operator wake: loops.reset(task, ResetCause.OPERATOR_WAKE)."""


@dataclass(frozen=True)
class ReleaseCard:
    """Kill: the board card goes back to Ready."""


@dataclass(frozen=True)
class CancelIssue:
    """Cancel: the card moves to Wont do, the issue closes as not planned."""


@dataclass(frozen=True)
class FinishMerged:
    """Close the issue, board Done, collect artifacts, remove the worktree
    and the branch, stamp done_at."""


@dataclass(frozen=True)
class SweepTombstone:
    """Delete the ended task's file for a fresh claim of its issue."""


@dataclass(frozen=True)
class Flush:
    """Retire a finished task past its retention window."""


@dataclass(frozen=True)
class TypeLoginCode:
    code: str


# ---- The table --------------------------------------------------------------

class Facts(NamedTuple):
    """What guards and builders read: the task's place and the event."""
    stage: Stage
    status: Status
    event: Event


Guard = Callable[[Facts], bool]


def always(f: Facts) -> bool:
    return True


def pr_closed(f: Facts) -> bool:
    return f.status.reason is FailReason.PR_CLOSED


def unknown_at_queued(f: Facts) -> bool:
    """A migrated failure with no known stage."""
    return f.status.reason is FailReason.UNKNOWN and f.stage is Stage.QUEUED


def ended_this_pass(f: Facts) -> bool:
    return f.event.this_pass


def launch_blocked(f: Facts) -> bool:
    return bool(f.event.blocked)


def respawns(f: Facts) -> bool:
    """The launch starts its stage afresh: after a failure, a claim, or the
    stage that follows implement."""
    origin = f.status.origin
    return origin is None or origin.kind is Kind.FAILED


def loop_exhausted(f: Facts) -> bool:
    return f.event.exhausted


def left_login_prompt(f: Facts) -> bool:
    return f.event.left_login_prompt


def _wake(cause: Cause) -> Callable[[Facts], Status]:
    return lambda f: Status(Kind.WAKING, origin=f.status, cause=cause)


def _review_park(f: Facts) -> Status:
    """The gate's page parked for review: the same request, no live session."""
    return Status(Kind.PARKED_REVIEW, path=f.status.path,
                  fingerprint=f.status.fingerprint, note=f.status.note)


def _wake_through_review(f: Facts) -> Status:
    """The gate session ends: the task passes through parked(review)."""
    return Status(Kind.WAKING, origin=_review_park(f), cause=Cause.REPLY)


_PR_ATTENTION = {EventType.PR_FEEDBACK: Cause.ATTENTION_FEEDBACK,
                 EventType.PR_CHECK_FAILED: Cause.ATTENTION_CHECK_FAILED,
                 EventType.PR_CONFLICT: Cause.ATTENTION_CONFLICT}


def _wake_for_pr(f: Facts) -> Status:
    return Status(Kind.WAKING, origin=f.status, cause=_PR_ATTENTION[f.event.type])


def _stay(f: Facts) -> Status:
    return f.status


def _human(reason: HumanReason, note: str = "") -> Callable[[Facts], Status]:
    return lambda f: Status(Kind.PARKED_HUMAN, reason=reason, note=note)


def _failed(reason: FailReason) -> Callable[[Facts], Status]:
    return lambda f: Status(Kind.FAILED, reason=reason)


def _respawn_here(f: Facts) -> RespawnStage:
    return RespawnStage(f.stage)


def _wake_blocked(f: Facts) -> WakeBlocked:
    return WakeBlocked(f.event.blocked)


def _resume_override(f: Facts) -> SetResumeOverride:
    return SetResumeOverride(f.event.model, f.event.bypass_usage)


def _login_code(f: Facts) -> TypeLoginCode:
    return TypeLoginCode(f.event.code)


Outcome = Kind | Ignore | Reject


@dataclass(frozen=True)
class Row:
    """(from status kind, event type, named guard, to, stage move, actions).
    `to` is the status kind a Transition enters, `build` its detail; an
    Ignore or a Reject row has neither. `move` (from stage, to stage) applies
    only at its from stage. An action is a value, or a function of the facts
    that makes one. The first row whose guard passes decides."""
    source: Kind
    event: EventType
    to: Outcome
    guard: Guard = always
    build: Callable[[Facts], Status] | None = None
    move: tuple[Stage, Stage] | None = None
    actions: tuple = ()

    def decides(self, f: Facts) -> bool:
        """Is this the row for `f`, its stage move and guard included?"""
        return (self.source is f.status.kind and self.event is f.event.type
                and (self.move is None or self.move[0] is f.stage) and self.guard(f))


def _rows(kinds, events, to: Outcome, **kw) -> list[Row]:
    return [Row(k, e, to, **kw) for k in kinds for e in events]


K, E = Kind, EventType
_ENDS = (K.FAILED, K.CANCELED, K.DONE)
_LIVE = tuple(k for k in K if k not in _ENDS)
_WAITS = (K.PARKED_HUMAN, K.PARKED_ANSWERS, K.PARKED_REVIEW, K.PARKED_ACCESS)
_REQUEST_WAITS = (K.PARKED_ANSWERS, K.PARKED_REVIEW)
_RESUMABLE_PARKS = _WAITS + (K.PARKED_CI,)
_PR_POKES = tuple(_PR_ATTENTION)
_OPERATOR_RESUME = (ResetRounds(), _resume_override)
_NOT_PARKED = Reject("the task is not parked")
_ENDED_REJECT = Reject("the task has ended")

TABLE: tuple[Row, ...] = tuple([
    # op-reply: the message is queued before decide; a wake delivers it.
    *_rows(_WAITS, [E.OP_REPLY], K.WAKING, build=_wake(Cause.REPLY), actions=(ResetRounds(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_REPLY, K.WAKING, build=_wake_through_review,
        actions=(EndSession(), ResetRounds())),
    *_rows((K.RUNNING, K.HELD_LOGIN, K.PARKED_CI, K.PARKED_PR, K.WAKING), [E.OP_REPLY],
           Ignore("the session reads the message on its next resume")),
    Row(K.FAILED, E.OP_REPLY, Reject("reopen the PR on GitHub or re-queue the issue"),
        guard=pr_closed),
    Row(K.FAILED, E.OP_REPLY, Ignore("the message waits for a resume")),
    *_rows((K.CANCELED, K.DONE), [E.OP_REPLY], Ignore("the task has ended")),

    # op-answers
    *_rows(_REQUEST_WAITS, [E.OP_ANSWERS], K.WAKING, build=_wake(Cause.REPLY),
           actions=(ResetRounds(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_ANSWERS, K.WAKING, build=_wake_through_review,
        actions=(EndSession(), ResetRounds())),
    Row(K.WAKING, E.OP_ANSWERS, Reject("session busy")),
    *_rows([k for k in K if k not in _REQUEST_WAITS + (K.HELD_PLAN_APPROVAL, K.WAKING)],
           [E.OP_ANSWERS], Reject("no open request")),

    # op-resume
    *_rows(_RESUMABLE_PARKS, [E.OP_RESUME], K.WAKING, build=_wake(Cause.RESUME),
           actions=_OPERATOR_RESUME),
    Row(K.FAILED, E.OP_RESUME, Reject("reopen the PR on GitHub or re-queue the issue"),
        guard=pr_closed),
    Row(K.FAILED, E.OP_RESUME, Reject("re-queue the issue instead"), guard=unknown_at_queued),
    Row(K.FAILED, E.OP_RESUME, K.WAKING, build=_wake(Cause.RESUME), actions=_OPERATOR_RESUME),
    Row(K.WAKING, E.OP_RESUME, K.WAKING, build=_stay, actions=(_resume_override,)),
    *_rows((K.RUNNING, K.HELD_PLAN_APPROVAL, K.HELD_LOGIN, K.PARKED_PR), [E.OP_RESUME],
           _NOT_PARKED),
    *_rows((K.CANCELED, K.DONE), [E.OP_RESUME], _ENDED_REJECT),

    # op-kill, op-cancel, op-park
    *_rows(_LIVE, [E.OP_KILL], K.FAILED, build=_failed(FailReason.KILLED),
           actions=(EndSession(), ReleaseCard())),
    *_rows(_ENDS, [E.OP_KILL], _ENDED_REJECT),
    *_rows(_LIVE + (K.FAILED,), [E.OP_CANCEL], K.CANCELED,
           actions=(EndSession(), CancelIssue())),
    Row(K.CANCELED, E.OP_CANCEL, Ignore("already canceled")),
    Row(K.DONE, E.OP_CANCEL, _ENDED_REJECT),
    Row(K.RUNNING, E.OP_PARK, K.PARKED_HUMAN, build=_human(HumanReason.OPERATOR, "parked by operator"),
        actions=(EndSession(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_PARK, K.PARKED_REVIEW, build=_review_park,
        actions=(EndSession(),)),
    *_rows([k for k in K if k not in (K.RUNNING, K.HELD_PLAN_APPROVAL)], [E.OP_PARK],
           Reject("only a running task or one at the plan gate can be parked")),

    # op-login-code: the pane is re-verified by the executor (left_login_prompt).
    Row(K.HELD_LOGIN, E.OP_LOGIN_CODE, Reject("no longer at a login prompt — code not typed"),
        guard=left_login_prompt),
    Row(K.HELD_LOGIN, E.OP_LOGIN_CODE, K.RUNNING, actions=(_login_code,)),
    *_rows([k for k in K if k is not K.HELD_LOGIN], [E.OP_LOGIN_CODE],
           Reject("the task is not held at a login prompt")),

    # resume-admitted: the launch mode (requirement 11).
    Row(K.WAKING, E.RESUME_ADMITTED, K.WAKING, guard=launch_blocked, build=_stay,
        actions=(_wake_blocked,)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, move=(Stage.PR_OPEN, Stage.ADDRESS_REVIEW),
        actions=(SpawnAddressReview(),)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, move=(Stage.QUEUED, Stage.SPEC),
        actions=(RespawnStage(Stage.SPEC),)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, guard=respawns, actions=(_respawn_here,)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, actions=(ContinueSession(),)),
    *_rows([k for k in K if k is not K.WAKING], [E.RESUME_ADMITTED], Ignore("not waking")),

    # candidate-ranked (requirement 12)
    *_rows((K.FAILED, K.CANCELED), [E.CANDIDATE_RANKED], Ignore("ended in this pass"),
           guard=ended_this_pass),
    Row(K.FAILED, E.CANDIDATE_RANKED, K.FAILED, guard=pr_closed, build=_stay,
        actions=(SweepTombstone(),)),
    Row(K.FAILED, E.CANDIDATE_RANKED, K.FAILED, guard=unknown_at_queued, build=_stay,
        actions=(SweepTombstone(),)),
    # as op-resume, with no override to set
    Row(K.FAILED, E.CANDIDATE_RANKED, K.WAKING, build=_wake(Cause.RANKED), actions=(ResetRounds(),)),
    Row(K.CANCELED, E.CANDIDATE_RANKED, K.CANCELED, actions=(SweepTombstone(),)),
    Row(K.DONE, E.CANDIDATE_RANKED, Reject("the task is done")),
    *_rows(_LIVE, [E.CANDIDATE_RANKED], Ignore("the issue has a live task")),

    # flush-due: the executor raises it past the retention window.
    *[Row(k, E.FLUSH_DUE, k, build=_stay, actions=(Flush(),)) for k in _ENDS],
    *_rows(_LIVE, [E.FLUSH_DUE], Ignore("not finished")),

    # ci-finished: a failed run spent a ci round (loops) before the event.
    Row(K.PARKED_CI, E.CI_FINISHED, K.PARKED_HUMAN, guard=loop_exhausted,
        build=_human(HumanReason.LOOP_CAP), actions=(Notify("parked_question"),)),
    Row(K.PARKED_CI, E.CI_FINISHED, K.WAKING, build=_wake(Cause.CI)),
    *_rows([k for k in K if k is not K.PARKED_CI], [E.CI_FINISHED], Ignore("not waiting for CI")),

    Row(K.PARKED_ACCESS, E.ACCESS_REACHED, K.WAKING, build=_wake(Cause.ACCESS)),
    *_rows([k for k in K if k is not K.PARKED_ACCESS], [E.ACCESS_REACHED],
           Ignore("not waiting for access")),

    # PR events (requirement 13)
    Row(K.PARKED_PR, E.PR_FEEDBACK, K.WAKING, build=_wake_for_pr,
        actions=(Notify("pr_feedback"),)),
    *_rows([K.PARKED_PR], (E.PR_CHECK_FAILED, E.PR_CONFLICT), K.PARKED_HUMAN,
           guard=loop_exhausted, build=_human(HumanReason.LOOP_CAP), actions=(Notify("parked_question"),)),
    *_rows([K.PARKED_PR], (E.PR_CHECK_FAILED, E.PR_CONFLICT), K.WAKING, build=_wake_for_pr,
           actions=(Notify("pr_attention"),)),
    *_rows([k for k in K if k is not K.PARKED_PR], _PR_POKES,
           Ignore("the PR is acted on only while parked on it")),
    *_rows(_LIVE, [E.PR_MERGED], K.DONE, actions=(EndSession(), FinishMerged(),
                                                 Notify("task_done"))),
    *_rows(_LIVE, [E.PR_CLOSED], K.FAILED, build=_failed(FailReason.PR_CLOSED),
           actions=(EndSession(), Notify("pr_closed"))),
    *_rows(_ENDS, (E.PR_MERGED, E.PR_CLOSED), Ignore("the task has ended")),
])


def decide(task: TaskState | At, event: Event) -> Transition | Ignore | Reject:
    """The table's answer to `event` for `task`. Pure: no file, clock,
    network or session."""
    at = _at(task)
    facts = Facts(at.stage, at.status, event)
    row = _match(facts)
    if not isinstance(row.to, Kind):
        return row.to
    status = row.build(facts) if row.build else Status(row.to)
    actions = tuple(a(facts) if callable(a) else a for a in row.actions)
    return Transition(status, row.move[1] if row.move else at.stage,
                      _with_slot(at.status, status, actions))


def _match(f: Facts) -> Row:
    return next(r for r in TABLE if r.decides(f))


def _with_slot(before: Status, after: Status, actions: tuple) -> tuple:
    """A slot is taken before the launch that needs it, and given back after
    the session ends."""
    if occupies(after) and not occupies(before):
        return (AcquireSlot(), *actions)
    if occupies(before) and not occupies(after):
        return (*actions, ReleaseSlot())
    return actions


class Edge(NamedTuple):
    source: str
    event: str
    guard: str
    to: str


PIPELINE = (Stage.QUEUED, Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW,
            Stage.PR_OPEN, Stage.ADDRESS_REVIEW)


def next_transitions(task: TaskState | At) -> list[Edge]:
    """The transitions `task` can take now: per event type, the row that
    decides it (with the event's plain facts), when that row is a transition."""
    at = _at(task)
    rows = (_match(Facts(at.stage, at.status, Event(e))) for e in EventType)
    return [_edge(r) for r in rows if isinstance(r.to, Kind)]


def graph() -> dict:
    """Every transition row as an edge between status kinds, and every stage
    move as an edge between stages."""
    rows = [r for r in TABLE if isinstance(r.to, Kind)]
    return {"status": {"nodes": [k.value for k in Kind], "edges": [_edge(r) for r in rows]},
            "stage": {"nodes": [s.value for s in PIPELINE], "edges": _stage_edges(rows)}}


def _stage_edges(rows: list[Row]) -> list[Edge]:
    return [Edge(r.move[0].value, r.event.value, r.guard.__name__, r.move[1].value)
            for r in rows if r.move]


def _edge(r: Row) -> Edge:
    return Edge(r.source.value, r.event.value, r.guard.__name__, r.to.value)
