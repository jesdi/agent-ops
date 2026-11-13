"""The task lifecycle (ADR 0014, specs/2026-11-10-task-lifecycle-state-machine).

A task is at a `stage` (pipeline position) with a `status` (what it does
there). Until the status is stored, `legacy_view` reads it from today's park
fields; the migration command applies the same function to every task file.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, NamedTuple

from dispatcher.artifacts import CheckResult, PlanRevision
from dispatcher.loops import Decision, Retry
from dispatcher.loops import Outcome as Rounds
from dispatcher.models import IMPLEMENT_PICK
from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PARK_WAKE, RESPAWNABLE_STAGES, WHY_ACCESS, WHY_BAD_SIGNAL,
                              WHY_CONFIG, WHY_DECISION, WHY_LOOP_CAP, WHY_OPERATOR,
                              WHY_STALLED, AnswersRequest,
                              OperatorRequest, PlanApprovalRequest, Stage, StageSignal,
                              TaskState, end, implement_done, park, unpark)


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
    """A live plan session at the review gate, held on its request; one that
    reworks the plan (its request disarmed) or was respawned runs."""
    if t.operator_request is None:
        return Status(Kind.RUNNING)
    if t.operator_request.kind != "plan-approval":
        raise ValueError(f"task {t.repository}#{t.issue}: at the plan gate with "
                         f"an {t.operator_request.kind} request")
    return Status(Kind.HELD_PLAN_APPROVAL, path=t.operator_request.path,
                  fingerprint=t.operator_request.fingerprint)


# The park_reason spelling of each HumanReason, the inverse of _HUMAN_REASONS.
_WHY = {HumanReason.DECISION: WHY_DECISION, HumanReason.STALLED: WHY_STALLED,
        HumanReason.LOOP_CAP: WHY_LOOP_CAP, HumanReason.BAD_SIGNAL: WHY_BAD_SIGNAL,
        HumanReason.CONFIG: WHY_CONFIG, HumanReason.OPERATOR: WHY_OPERATOR}


def legacy_fields(t: TaskState, stage: Stage, status: Status) -> TaskState:
    """`t` with (stage, status) written to today's park fields through the
    doors: the inverse of legacy_view for what the session events enter.
    Expand phase only (design "The move"); storing the status replaces it."""
    t = _without_implement_pick(t, stage)
    return _ENCODE[status.kind](t, stage, status)


def _without_implement_pick(t: TaskState, stage: Stage) -> TaskState:
    """Implement is done once its pick is gone (state.implement_done)."""
    if t.stage is not Stage.IMPLEMENT or stage is not Stage.REVIEW:
        return t
    return replace(t, picks={k: v for k, v in t.picks.items() if k != IMPLEMENT_PICK})


def _gate_stage(t: TaskState, stage: Stage) -> Stage:
    """A plan session at the gate, or of a task that waited there, runs there."""
    at_gate = t.gated or t.stage is Stage.AWAITING_PLAN_REVIEW
    return Stage.AWAITING_PLAN_REVIEW if stage is Stage.PLAN and at_gate else stage


def _at_stage(t: TaskState, stage: Stage) -> TaskState:
    return replace(t, stage=_gate_stage(t, stage))


def _encode_running(t: TaskState, stage: Stage, status: Status) -> TaskState:
    return unpark(_at_stage(t, stage))


def _encode_held(t: TaskState, stage: Stage, status: Status) -> TaskState:
    request = PlanApprovalRequest(status.path, fingerprint=status.fingerprint)
    return replace(unpark(t, request=request), stage=Stage.AWAITING_PLAN_REVIEW)


def _encode_login(t: TaskState, stage: Stage, status: Status) -> TaskState:
    return park(_at_stage(t, stage), PARK_LOGIN, note=status.note)


def _encode_human(t: TaskState, stage: Stage, status: Status) -> TaskState:
    return park(_at_stage(t, stage), PARK_HUMAN, note=status.note, reason=_WHY[status.reason])


def _encode_answers(t: TaskState, stage: Stage, status: Status) -> TaskState:
    request = AnswersRequest(status.path, fingerprint=status.fingerprint)
    return park(_at_stage(t, stage), PARK_HUMAN, note=status.note, reason=WHY_DECISION,
                request=request)


def _encode_review(t: TaskState, stage: Stage, status: Status) -> TaskState:
    request = PlanApprovalRequest(status.path, fingerprint=status.fingerprint)
    return replace(park(t, PARK_REVIEW, note=status.note, request=request),
                   stage=Stage.AWAITING_PLAN_REVIEW)


def _encode_ci(t: TaskState, stage: Stage, status: Status) -> TaskState:
    return park(_at_stage(t, stage), PARK_CI, ci_run_id=status.run_id)


def _encode_pr(t: TaskState, stage: Stage, status: Status) -> TaskState:
    return replace(unpark(t), stage=Stage.PR_OPEN)


def _encode_waking(t: TaskState, stage: Stage, status: Status) -> TaskState:
    """Only the wait for the review launch is entered by a session event."""
    if status.cause is not Cause.NEXT_STAGE:
        raise ValueError(f"no legacy shape for waking({status.cause})")
    return replace(unpark(t), stage=Stage.IMPLEMENT)


def _encode_failed(t: TaskState, stage: Stage, status: Status) -> TaskState:
    """A crash remembers the stage Resume respawns; any other failure is not
    resumable in the legacy shape."""
    launched = Stage.ADDRESS_REVIEW if stage is Stage.PR_OPEN else stage
    crashed = status.reason in (FailReason.SESSION_CRASH, FailReason.TASK_CRASH)
    return end(t, Stage.FAILED,
               crashed_stage=launched.value if crashed and launched in RESPAWNABLE_STAGES
               else "")


def _no_encoding(t: TaskState, stage: Stage, status: Status) -> TaskState:
    raise ValueError(f"no session event enters {status.kind.value}")


_ENCODE = {Kind.RUNNING: _encode_running, Kind.HELD_PLAN_APPROVAL: _encode_held,
           Kind.HELD_LOGIN: _encode_login, Kind.PARKED_HUMAN: _encode_human,
           Kind.PARKED_ANSWERS: _encode_answers, Kind.PARKED_ACCESS: _no_encoding,
           Kind.PARKED_CI: _encode_ci, Kind.PARKED_REVIEW: _encode_review,
           Kind.PARKED_PR: _encode_pr, Kind.WAKING: _encode_waking,
           Kind.FAILED: _encode_failed, Kind.CANCELED: _no_encoding,
           Kind.DONE: _no_encoding}



# ---- Events ---------------------------------------------------------------

class EventType(str, Enum):
    """What can happen to a task (requirement 6)."""
    SESSION_SIGNAL = "session-signal"
    SESSION_DIED = "session-died"
    SESSION_IDLE = "session-idle"
    SESSION_WAITING = "session-waiting"
    LAUNCH_FAILED = "launch-failed"
    TASK_CRASHED = "task-crashed"
    GRACE_EXPIRED = "grace-expired"
    TRACK_UNCONFIGURED = "track-unconfigured"
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


class Pane(str, Enum):
    """What herdr and the pane say right before a poke is typed."""
    UNSEEN = ""          # not looked at yet: a poke is due
    CLEAR = "clear"      # a stopped session at its composer
    UNKNOWN = "unknown"  # herdr or the pane cannot be read: hold
    WORKING = "working"  # the session started a turn after the idle read
    PROMPT = "prompt"    # a permission, trust or login prompt


@dataclass(frozen=True)
class Session:
    """What the rules of a session event read: the stage signal and the facts
    classify gathered from the task and the worktree for it."""
    signal: StageSignal | None = None
    round: Decision | None = None        # a working report's round the policy counts
    at_gate: bool = False                # the plan session runs at the review gate
    gated: bool = False                  # the task waited at the gate once
    retries: frozenset[Retry] = frozenset()   # the budgets with a retry left
    rounds_used: bool = False            # the gate's unattended rounds are used up
    respawn_pending: bool = False        # a gate respawn prepaid the next round
    armed: bool = False                  # the request is armed for the plan on disk
    skips_gate: bool = False
    asks: bool = False                   # a park for answers marks the task as asking
    bad_track: str = ""                  # why a done report's track is refused
    tickets: CheckResult | None = None
    revision: PlanRevision | None = None
    spec: CheckResult | None = None
    answers: tuple[str, str] | None = None   # the answers page: (path, fingerprint)


@dataclass(frozen=True)
class Event:
    """An event and the facts the executor read for it (decide reads no file,
    clock or session). Every default is the plain case; a fact is set only on
    the event types of _CARRIED."""
    type: EventType
    blocked: str = ""              # why the launch cannot start now
    exhausted: bool = False        # no ci round left
    this_pass: bool = False        # the task ended in this pass
    left_login_prompt: bool = False  # the pane shows no login prompt any more
    model: str = ""                # the model override
    bypass_usage: bool = False     # launch past the usage gate
    code: str = field(default="", repr=False)  # never logged
    session: Session | None = None   # what the session's rules read
    note: str = ""                 # the question a park asks the operator
    launch_denied: bool = False    # the launch the rule names is not admitted now
    login: str = ""                # the pane is at a login prompt: its URL
    pane: Pane = Pane.UNSEEN       # the look before a poke
    record: tuple[float, int] | None = None  # a background report to record: (reported, seq)
    stage: Stage | None = None     # the stage whose launch raised

    def __post_init__(self) -> None:
        stray = [name for name, types in _CARRIED.items()
                 if getattr(self, name) and self.type not in types]
        if stray:
            raise ValueError(f"{self.type.value} carries no {', '.join(stray)}")


def carries(event_type: EventType, fact: str) -> bool:
    """Can an event of `event_type` carry `fact`?"""
    return event_type in _CARRIED[fact]


_RUNNING_SESSION = {EventType.SESSION_SIGNAL, EventType.SESSION_IDLE,
                    EventType.SESSION_WAITING}
_CARRIED = {"blocked": {EventType.RESUME_ADMITTED},
            "exhausted": {EventType.CI_FINISHED, EventType.PR_CHECK_FAILED,
                          EventType.PR_CONFLICT},
            "this_pass": {EventType.CANDIDATE_RANKED},
            "left_login_prompt": {EventType.OP_LOGIN_CODE},
            "model": {EventType.OP_RESUME}, "bypass_usage": {EventType.OP_RESUME},
            "code": {EventType.OP_LOGIN_CODE},
            "session": {*_RUNNING_SESSION, EventType.SESSION_DIED},
            "note": {*_RUNNING_SESSION, EventType.TRACK_UNCONFIGURED},
            "launch_denied": {EventType.SESSION_SIGNAL, EventType.SESSION_DIED},
            "login": {*_RUNNING_SESSION, EventType.TRACK_UNCONFIGURED},
            "pane": {EventType.SESSION_IDLE},
            "record": {EventType.SESSION_WAITING},
            "stage": {EventType.LAUNCH_FAILED}}


# ---- Outcomes and actions ---------------------------------------------------

@dataclass(frozen=True)
class Transition:
    status: Status
    stage: Stage
    actions: tuple[Action, ...] = ()


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
    """Start `stage` afresh in the same worktree, never --continue.
    `tickets`: the checked ticket set an implement start records."""
    stage: Stage
    tickets: int = 0


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
    code: str = field(repr=False)  # never logged


@dataclass(frozen=True)
class Notify:
    """Sent after the commit, never for a transition that did not commit."""
    template: str
    note: str = ""


@dataclass(frozen=True)
class RetryStage:
    """Resume the session in place with why its signal was rejected,
    spending one retry of `retry` (loops.Retry)."""
    stage: Stage
    reason: str
    retry: Retry


@dataclass(frozen=True)
class PublishSpec:
    """Commit and push the task's spec folder; `review` also links it on the
    issue as a plan that waits for review."""
    review: bool = True


@dataclass(frozen=True)
class ApplyRound:
    """Write a loop round's counter (loops.Decision.apply_to)."""
    decision: Decision


POKE_TEXT = ("Check your background terminals. If the work finished, continue the stage. "
             "If it is still running, wait for it. If you need the operator, set the stage "
             "status to blocked with a note.")


@dataclass(frozen=True)
class Poke:
    """Type `text` into a stopped session's pane."""
    text: str = POKE_TEXT


@dataclass(frozen=True)
class EndPokeStretch:
    """The live session moved on: the next poke starts a new clock."""


@dataclass(frozen=True)
class RecordBackgroundWait:
    """Record the background report seen with herdr's counter at `seq`."""
    reported: float
    seq: int


@dataclass(frozen=True)
class ReportCrash:
    """A session died mid-stage: release the card, file the failure report."""


@dataclass(frozen=True)
class ReportTaskCrash:
    """A task's turn raised: release the card, file the failure report."""


@dataclass(frozen=True)
class MarkAsked:
    """A spec or plan session parked for answers (TaskState.asked)."""


@dataclass(frozen=True)
class EnterGate:
    """Record a review round at the plan gate (loops.gate_fields)."""


@dataclass(frozen=True)
class LinkPR:
    """Record the PR the review signal links."""


@dataclass(frozen=True)
class ClearTurnMarkers:
    """Drop the session's waiting and background markers and its poke
    stretch: the pane stays, its turn is over."""


Action = (AcquireSlot | ReleaseSlot | EndSession | ContinueSession | RespawnStage
          | SpawnAddressReview | WakeBlocked | SetResumeOverride | ResetRounds | ReleaseCard
          | CancelIssue | FinishMerged | SweepTombstone | Flush | TypeLoginCode | Notify
          | RetryStage | PublishSpec | ApplyRound | Poke | EndPokeStretch
          | RecordBackgroundWait | ReportCrash | ReportTaskCrash | MarkAsked | EnterGate
          | LinkPR | ClearTurnMarkers)


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


def next_stage(f: Facts) -> bool:
    """Review waits to launch after implement."""
    return f.status.cause is Cause.NEXT_STAGE


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


# ---- The stage sub-function: a running session's report, death and silence ---

PLAN_NO_APPROVAL = ('status "done" is accepted only after the operator has '
                    'approved the plan at the review gate; write the review '
                    'page and report status "awaiting-review" with the '
                    'page path as "artifact", then wait for the reply')
SPEC_NO_REVIEW = ('the spec stage has no review gate and status '
                  '"awaiting-review" is not valid in it; once stage 1 is '
                  'committed and pushed, report status "done" with the '
                  'spec.md path as "artifact" and a "track"')
STOPPED_NOTE = "(session stopped mid-stage waiting for input)"
GATE_STOPPED = "plan session stopped at the review gate"
PLAN_READY = "plan ready for review"
_WAITS_FOR_LAUNCH = Ignore("the launch waits for admission")


def _session(f: Facts) -> Session:
    return f.event.session or Session()


def _asking(f: Facts, reason: HumanReason, note: str, *before: Action) -> Transition:
    """The session ends and the operator is asked."""
    return Transition(Status(Kind.PARKED_HUMAN, reason=reason, note=note), f.stage,
                      (*before, EndSession(), Notify("parked_question", note)))


def _review_parked(f: Facts, path: str, fingerprint: str, note: str,
                   template: str) -> Transition:
    """The plan waits for review with no session: its slot goes back."""
    return Transition(Status(Kind.PARKED_REVIEW, path=path, fingerprint=fingerprint,
                             note=note), f.stage,
                      (PublishSpec(review=False), EndSession(), Notify(template)))


def _failed_artifact(f: Facts, reason: str) -> Transition:
    return Transition(Status(Kind.FAILED, reason=FailReason.ARTIFACT), f.stage,
                      (ClearTurnMarkers(), Notify("artifact_failed", reason)))


def _rounds(f: Facts) -> tuple[Action, ...]:
    d = _session(f).round
    return (ApplyRound(d),) if d else ()


def _exhausted(f: Facts) -> Transition | None:
    """A loop round past its cap parks before anything else the event does."""
    d = _session(f).round
    if d is None or d.outcome is not Rounds.EXHAUSTED:
        return None
    note = f"{d.loop.value} loop exceeded its cap of {d.cap} rounds"
    return _asking(f, HumanReason.LOOP_CAP, note + (f" ({d.detail})" if d.detail else ""),
                   ApplyRound(d))


def _stalled(f: Facts, reason: HumanReason, note: str) -> Transition:
    return _exhausted(f) or _asking(f, reason, note, *_rounds(f))


def _working(f: Facts, *then: Action) -> Transition | Ignore:
    """A working session: its round counted, a request at the gate disarmed
    (the plan is being reworked), then `then`."""
    exhausted = _exhausted(f)
    if exhausted:
        return exhausted
    status = Status(Kind.RUNNING) if f.status.kind is Kind.HELD_PLAN_APPROVAL else f.status
    actions = (*_rounds(f), *then)
    if not actions and status == f.status:
        return Ignore("the session works")
    return Transition(status, f.stage, actions)


def _bounce(f: Facts, retry: Retry, reason: str) -> Transition | Ignore:
    """A rejected signal: the session is resumed with the reason while its
    retry lasts, then the task parks."""
    if retry not in _session(f).retries:
        return _asking(f, HumanReason.BAD_SIGNAL, reason)
    if f.event.launch_denied:   # the plan on offer is rejected all the same
        return _disarmed(f) if f.status.kind is Kind.HELD_PLAN_APPROVAL else _WAITS_FOR_LAUNCH
    told = (Notify("plan_retry", reason),) if f.stage is Stage.PLAN else ()
    return Transition(Status(Kind.RUNNING), f.stage,
                      (RetryStage(f.stage, reason, retry), *told))


def _disarmed(f: Facts) -> Transition:
    return Transition(Status(Kind.RUNNING), f.stage)


def _check_failed(f: Facts, result: CheckResult) -> Transition | Ignore | None:
    """A ticket set (or plan revision) that fails its check: retried in the
    session once, then the task fails. None when it passes."""
    if result.ok:
        return None
    if Retry.PLAN_TICKETS in _session(f).retries:
        return _bounce(f, Retry.PLAN_TICKETS, result.reason)
    return _failed_artifact(f, result.reason)


def _plan_check(s: Session) -> CheckResult:
    """The ticket check, and that the plan reads as one revision."""
    tickets = s.tickets or CheckResult(False, "no ticket check")
    problem = s.revision.problem if s.revision else "no plan revision"
    return CheckResult(False, problem) if tickets.ok and problem else tickets


def _implement(f: Facts, *before: Action) -> Transition | Ignore:
    """One implement session works the checked ticket set."""
    if f.event.launch_denied:
        return _WAITS_FOR_LAUNCH
    n = _session(f).tickets.count
    return Transition(Status(Kind.RUNNING), Stage.IMPLEMENT,
                      (*before, RespawnStage(Stage.IMPLEMENT, tickets=n),
                       Notify("implement_started", f"{n} ticket(s)")))


def _review_round(f: Facts) -> Transition:
    """The plan is held at the gate for the operator, the session kept."""
    s = _session(f)
    held = Status(Kind.HELD_PLAN_APPROVAL, path=s.revision.path,
                  fingerprint=s.revision.fingerprint)
    return Transition(held, Stage.PLAN, (EnterGate(), PublishSpec(),
                                         Notify("awaiting_plan_review", s.signal.note)))


def _plan_ready(f: Facts) -> Transition | Ignore:
    """The plan stage's ready report: nothing reaches the operator, and no
    implement session starts, before the ticket check."""
    s = _session(f)
    failed = _check_failed(f, _plan_check(s))
    if failed:
        return failed
    if s.skips_gate:
        return _implement(f, PublishSpec(review=False))
    return _review_round(f)


def _gate_ready(f: Facts) -> Transition | Ignore:
    """A ready report at the gate: an armed plan waits for the operator (or
    its grace time, GRACE_EXPIRED); anything else is a new review round while
    unattended rounds are left."""
    s = _session(f)
    if s.armed:
        return Ignore("the armed plan waits for the operator")
    failed = _check_failed(f, _plan_check(s))
    if failed:
        return failed
    if s.rounds_used and not s.respawn_pending:
        return _review_parked(f, s.revision.path, s.revision.fingerprint, PLAN_READY,
                              "plan_parked")
    return _review_round(f)


def _ready(f: Facts) -> Transition | Ignore:
    """awaiting-review, by the stage it arrives in."""
    if _session(f).at_gate:
        return _gate_ready(f)
    if f.stage is Stage.PLAN:
        return _plan_ready(f)
    if f.stage is Stage.SPEC:
        return _bounce(f, Retry.SPEC_SIGNAL, SPEC_NO_REVIEW)
    return Ignore("the stage has no review gate")


def _approved(f: Facts) -> Transition | Ignore:
    """The operator's approval, reported by the plan session at the gate."""
    s = _session(f)
    if s.bad_track:
        return _bounce(f, Retry.PLAN_SIGNAL, s.bad_track)
    return _check_failed(f, s.tickets or CheckResult(False, "no ticket check")) \
        or _implement(f)


def _plan_done(f: Facts) -> Transition | Ignore:
    s = _session(f)
    if s.at_gate:
        return _approved(f)
    if s.gated:
        return _asking(f, HumanReason.BAD_SIGNAL, (
            f"The gate state was lost: this task waited at the plan gate, "
            f"but it is in stage plan and reports done with track "
            f"{s.signal.track or 'none'!r}. Move it to implement by hand."))
    return _bounce(f, Retry.PLAN_SIGNAL, PLAN_NO_APPROVAL)


def _spec_done(f: Facts) -> Transition | Ignore:
    s = _session(f)
    if s.bad_track:
        return _bounce(f, Retry.SPEC_SIGNAL, s.bad_track)
    if not s.spec.ok:
        return _failed_artifact(f, s.spec.reason)
    if f.event.launch_denied:
        return _WAITS_FOR_LAUNCH
    return Transition(Status(Kind.RUNNING), Stage.PLAN, (RespawnStage(Stage.PLAN),))


def _implement_done(f: Facts) -> Transition:
    """Review follows implement; a denied launch waits as waking(next-stage)."""
    if f.event.launch_denied:
        return Transition(Status(Kind.WAKING, cause=Cause.NEXT_STAGE), Stage.REVIEW)
    return Transition(Status(Kind.RUNNING), Stage.REVIEW,
                      (RespawnStage(Stage.REVIEW),
                       Notify("review_started", _session(f).signal.note)))


def _pr_opened(template: str) -> Callable[[Facts], Transition]:
    def opened(f: Facts) -> Transition:
        return Transition(Status(Kind.PARKED_PR), Stage.PR_OPEN,
                          (LinkPR(), Notify(template, _session(f).signal.note)))
    return opened


_DONE: dict[Stage, Callable[[Facts], Transition | Ignore]] = {
    Stage.SPEC: _spec_done, Stage.PLAN: _plan_done, Stage.IMPLEMENT: _implement_done,
    Stage.REVIEW: _pr_opened("pr_opened"), Stage.ADDRESS_REVIEW: _pr_opened("pr_updated")}


def _done(f: Facts) -> Transition | Ignore:
    rule = _DONE.get(f.stage)
    return rule(f) if rule else Ignore("no stage follows")


def _awaiting_ci(f: Facts) -> Transition:
    run_id = _session(f).signal.run_id
    if run_id <= 0:
        return _failed_artifact(f, "awaiting-ci without run_id")
    return Transition(Status(Kind.PARKED_CI, run_id=run_id), f.stage, (EndSession(),))


def _blocked(f: Facts) -> Transition:
    return _asking(f, HumanReason.DECISION, _session(f).signal.note)


def _awaiting_answers(f: Facts) -> Transition:
    """A questionnaire, prototype or wizard: the page the operator answers."""
    s = _session(f)
    asked = (MarkAsked(),) if s.asks else ()
    note = s.signal.note
    if s.answers is None:
        note = (note + "\n\n[malformed awaiting-answers: no usable artifact path]").strip()
        return _asking(f, HumanReason.DECISION, note, *asked)
    path, fingerprint = s.answers
    return Transition(Status(Kind.PARKED_ANSWERS, path=path, fingerprint=fingerprint,
                             note=note), f.stage,
                      (*asked, EndSession(), Notify("parked_question", note)))


_SIGNALS: dict[str, Callable[[Facts], Transition | Ignore]] = {
    "awaiting-ci": _awaiting_ci, "blocked": _blocked, "awaiting-answers": _awaiting_answers,
    "working": _working, "awaiting-review": _ready, "done": _done}


def on_signal(f: Facts) -> Transition | Ignore:
    """running + session-signal (requirement 15): today's rules by status."""
    signal = _session(f).signal
    if signal is None:
        return Ignore("no signal")
    rule = _SIGNALS.get(signal.status)
    return rule(f) if rule else Ignore(f"unknown signal status {signal.status!r}")


_POKE = {Pane.UNSEEN: (Poke(),), Pane.CLEAR: (Poke(),), Pane.UNKNOWN: (),
         Pane.WORKING: (EndPokeStretch(),)}


def on_idle(f: Facts) -> Transition | Ignore:
    """A silent session parks stalled (the note says why); a stopped one is
    poked, unless the look before the poke finds it at a prompt."""
    if f.event.note:
        return _stalled(f, HumanReason.STALLED, f.event.note)
    if f.event.pane is Pane.PROMPT:
        return _stalled(f, HumanReason.DECISION, STOPPED_NOTE)
    return _working(f, *_POKE[f.event.pane])


def on_waiting(f: Facts) -> Transition | Ignore:
    """A background wait holds the task running; past its cap it parks."""
    if f.event.note:
        return _stalled(f, HumanReason.STALLED, f.event.note)
    recorded = (RecordBackgroundWait(*f.event.record),) if f.event.record else ()
    actions = (*_rounds(f), *recorded)
    if not actions:
        return Ignore("the background wait holds")
    return _exhausted(f) or Transition(f.status, f.stage, actions)


def on_died(f: Facts) -> Transition | Ignore:
    """A dead session crashes its stage; at the gate the plan session is
    respawned while unattended rounds remain."""
    if _session(f).at_gate:
        return _gate_died(f)
    return Transition(Status(Kind.FAILED, reason=FailReason.SESSION_CRASH), f.stage,
                      (ReportCrash(), EndSession(), Notify("session_crashed")))


def _gate_died(f: Facts) -> Transition | Ignore:
    if not _session(f).rounds_used:
        if f.event.launch_denied:
            return _WAITS_FOR_LAUNCH
        return Transition(Status(Kind.RUNNING), Stage.PLAN, (RespawnStage(Stage.PLAN),))
    if not f.status.path:   # a rework session: no plan is on offer
        return Transition(Status(Kind.PARKED_HUMAN, reason=HumanReason.STALLED,
                                 note=GATE_STOPPED), f.stage,
                          (EndSession(), Notify("plan_session_stopped")))
    return _review_parked(f, f.status.path, f.status.fingerprint, GATE_STOPPED,
                          "plan_session_stopped")


def on_launch_failed(f: Facts) -> Transition:
    """The hand-over is accepted; only the launch failed: the task fails at
    the stage that could not start, which Resume starts."""
    return Transition(Status(Kind.FAILED, reason=FailReason.TASK_CRASH),
                      f.event.stage or f.stage, (ReportTaskCrash(),))


def _grace_park(f: Facts) -> Status:
    return Status(Kind.PARKED_REVIEW, path=f.status.path,
                  fingerprint=f.status.fingerprint, note=PLAN_READY)


def _config_park(f: Facts) -> Status:
    return Status(Kind.PARKED_HUMAN, reason=HumanReason.CONFIG, note=f.event.note)


def _config_question(f: Facts) -> Notify:
    return Notify("parked_question", f.event.note)


@dataclass(frozen=True)
class Via:
    """A row whose answer a rule function gives from the facts (the stage
    sub-function); `kinds` are the statuses it can enter."""
    rule: Callable[[Facts], Transition | Ignore]
    kinds: tuple[Kind, ...]

    @property
    def value(self) -> str:
        return self.rule.__name__


Outcome = Kind | Ignore | Reject | Via


@dataclass(frozen=True)
class Row:
    """(from status kind, event type, named guard, to, stage move, actions).
    `to` is the status kind a Transition enters, `build` its detail. `move`
    (from stage, to stage) applies only at its from stage. `actions` are
    fixed; `derive` makes more from the facts, emitted after them. An Ignore
    or a Reject row has no build, move or actions. The first row whose guard
    passes decides."""
    source: Kind
    event: EventType
    to: Outcome
    guard: Guard = always
    build: Callable[[Facts], Status] | None = None
    move: tuple[Stage, Stage] | None = None
    actions: tuple[Action, ...] = ()
    derive: tuple[Callable[[Facts], Action], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.to, Kind) and any((self.build, self.move, self.actions,
                                                   self.derive)):
            raise ValueError(f"{self.source.value} + {self.event.value}: a row that "
                             f"decides nothing takes no build, move or actions")

    def decides(self, f: Facts) -> bool:
        """Is this the row for `f`, its stage move and guard included?"""
        return (self.source is f.status.kind and self.event is f.event.type
                and (self.move is None or self.move[0] is f.stage) and self.guard(f))


    @property
    def unconditional(self) -> bool:
        """Does the row decide its pair whatever the facts and the stage?"""
        return self.guard is always and self.move is None


def _rows(kinds, events, to: Outcome, **kw) -> list[Row]:
    return [Row(k, e, to, **kw) for k in kinds for e in events]


@dataclass(frozen=True)
class _Rest:
    """`to` for every kind no row above decides unconditionally for `event`."""
    event: EventType
    to: Ignore | Reject


def _rest(events, to: Ignore | Reject) -> list[_Rest]:
    return [_Rest(e, to) for e in events]


def _complete(entries: list[Row | _Rest]) -> tuple[Row, ...]:
    """The rows, each _Rest expanded against the rows before it."""
    rows: list[Row] = []
    for entry in entries:
        rows += _remaining(rows, entry) if isinstance(entry, _Rest) else [entry]
    return tuple(rows)


def _remaining(rows: list[Row], rest: _Rest) -> list[Row]:
    decided = {r.source for r in rows if r.event is rest.event and r.unconditional}
    return [Row(k, rest.event, rest.to) for k in Kind if k not in decided]


K, E = Kind, EventType
_ENDS = (K.FAILED, K.CANCELED, K.DONE)
_LIVE = tuple(k for k in K if k not in _ENDS)
_WAITS = (K.PARKED_HUMAN, K.PARKED_ANSWERS, K.PARKED_REVIEW, K.PARKED_ACCESS)
_REQUEST_WAITS = (K.PARKED_ANSWERS, K.PARKED_REVIEW)
_RESUMABLE_PARKS = _WAITS + (K.PARKED_CI,)
_PR_POKES = tuple(_PR_ATTENTION)
_ENDED_REJECT = Reject("the task has ended")

_SESSIONS = (K.RUNNING, K.HELD_PLAN_APPROVAL)   # a session runs that a pass drives
_DRIVEN = (*_SESSIONS, K.WAKING)
_NO_SESSION = Ignore("no session runs")
_SIGNALLED = (K.RUNNING, K.HELD_PLAN_APPROVAL, K.HELD_LOGIN, K.PARKED_HUMAN,
              K.PARKED_ANSWERS, K.PARKED_CI, K.PARKED_REVIEW, K.PARKED_PR, K.WAKING,
              K.FAILED)
_PARKS = (K.RUNNING, K.HELD_PLAN_APPROVAL, K.HELD_LOGIN, K.PARKED_HUMAN, K.PARKED_REVIEW)

TABLE: tuple[Row, ...] = _complete([
    # Session events (requirements 15, 20). classify picks one per pass.
    *_rows(_SESSIONS, [E.SESSION_SIGNAL], Via(on_signal, _SIGNALLED)),
    *_rest([E.SESSION_SIGNAL], _NO_SESSION),
    *_rows(_SESSIONS, [E.SESSION_IDLE], Via(on_idle, _PARKS)),
    *_rest([E.SESSION_IDLE], _NO_SESSION),
    *_rows(_SESSIONS, [E.SESSION_WAITING], Via(on_waiting, _PARKS)),
    *_rest([E.SESSION_WAITING], _NO_SESSION),
    *_rows(_SESSIONS, [E.SESSION_DIED],
           Via(on_died, (K.FAILED, K.RUNNING, K.PARKED_REVIEW, K.PARKED_HUMAN))),
    *_rest([E.SESSION_DIED], _NO_SESSION),
    Row(K.HELD_PLAN_APPROVAL, E.GRACE_EXPIRED, K.PARKED_REVIEW, build=_grace_park,
        actions=(PublishSpec(review=False), EndSession(), Notify("plan_parked"))),
    *_rest([E.GRACE_EXPIRED], Ignore("not held at the plan gate")),
    *_rows(_DRIVEN, [E.TRACK_UNCONFIGURED], K.PARKED_HUMAN, build=_config_park,
           actions=(EndSession(),), derive=(_config_question,)),
    *_rest([E.TRACK_UNCONFIGURED], Ignore("not driven")),
    *_rows(_DRIVEN, [E.LAUNCH_FAILED], Via(on_launch_failed, (K.FAILED,))),
    *_rest([E.LAUNCH_FAILED], Ignore("no launch was due")),
    *_rows(_LIVE, [E.TASK_CRASHED], K.FAILED, build=_failed(FailReason.TASK_CRASH),
           actions=(ReportTaskCrash(),)),
    *_rest([E.TASK_CRASHED], Ignore("the task has ended")),

    # op-reply: the message is queued before decide; a wake delivers it.
    *_rows(_WAITS, [E.OP_REPLY], K.WAKING, build=_wake(Cause.REPLY), actions=(ResetRounds(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_REPLY, K.WAKING, build=_wake_through_review,
        actions=(EndSession(), ResetRounds())),
    Row(K.FAILED, E.OP_REPLY, Reject("reopen the PR on GitHub or re-queue the issue"),
        guard=pr_closed),
    Row(K.FAILED, E.OP_REPLY, Ignore("the message waits for a resume")),
    *_rows((K.CANCELED, K.DONE), [E.OP_REPLY], Ignore("the task has ended")),
    *_rest([E.OP_REPLY], Ignore("the session reads the message on its next resume")),

    # op-answers
    *_rows(_REQUEST_WAITS, [E.OP_ANSWERS], K.WAKING, build=_wake(Cause.REPLY),
           actions=(ResetRounds(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_ANSWERS, K.WAKING, build=_wake_through_review,
        actions=(EndSession(), ResetRounds())),
    Row(K.WAKING, E.OP_ANSWERS, Reject("session busy")),
    *_rest([E.OP_ANSWERS], Reject("no open request")),

    # op-resume
    *_rows(_RESUMABLE_PARKS, [E.OP_RESUME], K.WAKING, build=_wake(Cause.RESUME),
           actions=(ResetRounds(),), derive=(_resume_override,)),
    Row(K.FAILED, E.OP_RESUME, Reject("reopen the PR on GitHub or re-queue the issue"),
        guard=pr_closed),
    Row(K.FAILED, E.OP_RESUME, Reject("re-queue the issue instead"), guard=unknown_at_queued),
    Row(K.FAILED, E.OP_RESUME, K.WAKING, build=_wake(Cause.RESUME), actions=(ResetRounds(),),
        derive=(_resume_override,)),
    Row(K.WAKING, E.OP_RESUME, K.WAKING, build=_stay, derive=(_resume_override,)),
    *_rows((K.CANCELED, K.DONE), [E.OP_RESUME], _ENDED_REJECT),
    *_rest([E.OP_RESUME], Reject("the task is not parked")),

    # op-kill, op-cancel, op-park
    *_rows(_LIVE, [E.OP_KILL], K.FAILED, build=_failed(FailReason.KILLED),
           actions=(EndSession(), ReleaseCard())),
    *_rest([E.OP_KILL], _ENDED_REJECT),
    *_rows(_LIVE + (K.FAILED,), [E.OP_CANCEL], K.CANCELED,
           actions=(EndSession(), CancelIssue())),
    Row(K.CANCELED, E.OP_CANCEL, Ignore("already canceled")),
    *_rest([E.OP_CANCEL], _ENDED_REJECT),
    Row(K.RUNNING, E.OP_PARK, K.PARKED_HUMAN,
        build=_human(HumanReason.OPERATOR, "parked by operator"),
        actions=(EndSession(),)),
    Row(K.HELD_PLAN_APPROVAL, E.OP_PARK, K.PARKED_REVIEW, build=_review_park,
        actions=(EndSession(),)),
    *_rest([E.OP_PARK], Reject("only a running task or one at the plan gate can be parked")),

    # op-login-code: the pane is re-verified by the executor (left_login_prompt).
    Row(K.HELD_LOGIN, E.OP_LOGIN_CODE, Reject("no longer at a login prompt — code not typed"),
        guard=left_login_prompt),
    Row(K.HELD_LOGIN, E.OP_LOGIN_CODE, K.RUNNING, derive=(_login_code,)),
    *_rest([E.OP_LOGIN_CODE], Reject("the task is not held at a login prompt")),

    # resume-admitted: the launch mode (requirement 11).
    Row(K.WAKING, E.RESUME_ADMITTED, K.WAKING, guard=launch_blocked, build=_stay,
        derive=(_wake_blocked,)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, move=(Stage.PR_OPEN, Stage.ADDRESS_REVIEW),
        actions=(SpawnAddressReview(),)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, move=(Stage.QUEUED, Stage.SPEC),
        actions=(RespawnStage(Stage.SPEC),)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, guard=next_stage,
        actions=(RespawnStage(Stage.REVIEW), Notify("review_started"))),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, guard=respawns, derive=(_respawn_here,)),
    Row(K.WAKING, E.RESUME_ADMITTED, K.RUNNING, actions=(ContinueSession(),)),
    *_rest([E.RESUME_ADMITTED], Ignore("not waking")),

    # candidate-ranked (requirement 12)
    *_rows((K.FAILED, K.CANCELED), [E.CANDIDATE_RANKED], Ignore("ended in this pass"),
           guard=ended_this_pass),
    Row(K.FAILED, E.CANDIDATE_RANKED, K.FAILED, guard=pr_closed, build=_stay,
        actions=(SweepTombstone(),)),
    Row(K.FAILED, E.CANDIDATE_RANKED, K.FAILED, guard=unknown_at_queued, build=_stay,
        actions=(SweepTombstone(),)),
    # as op-resume, with no override to set
    Row(K.FAILED, E.CANDIDATE_RANKED, K.WAKING, build=_wake(Cause.RANKED),
        actions=(ResetRounds(),)),
    Row(K.CANCELED, E.CANDIDATE_RANKED, K.CANCELED, actions=(SweepTombstone(),)),
    Row(K.DONE, E.CANDIDATE_RANKED, Reject("the task is done")),
    *_rest([E.CANDIDATE_RANKED], Ignore("the issue has a live task")),

    # flush-due: the executor raises it past the retention window.
    *[Row(k, E.FLUSH_DUE, k, build=_stay, actions=(Flush(),)) for k in _ENDS],
    *_rest([E.FLUSH_DUE], Ignore("not finished")),

    # ci-finished: a failed run spent a ci round (loops) before the event.
    Row(K.PARKED_CI, E.CI_FINISHED, K.PARKED_HUMAN, guard=loop_exhausted,
        build=_human(HumanReason.LOOP_CAP), actions=(Notify("parked_question"),)),
    Row(K.PARKED_CI, E.CI_FINISHED, K.WAKING, build=_wake(Cause.CI)),
    *_rest([E.CI_FINISHED], Ignore("not waiting for CI")),

    Row(K.PARKED_ACCESS, E.ACCESS_REACHED, K.WAKING, build=_wake(Cause.ACCESS)),
    *_rest([E.ACCESS_REACHED], Ignore("not waiting for access")),

    # PR events (requirement 13)
    Row(K.PARKED_PR, E.PR_FEEDBACK, K.WAKING, build=_wake_for_pr,
        actions=(Notify("pr_feedback"),)),
    *_rows([K.PARKED_PR], (E.PR_CHECK_FAILED, E.PR_CONFLICT), K.PARKED_HUMAN,
           guard=loop_exhausted, build=_human(HumanReason.LOOP_CAP),
           actions=(Notify("parked_question"),)),
    *_rows([K.PARKED_PR], (E.PR_CHECK_FAILED, E.PR_CONFLICT), K.WAKING, build=_wake_for_pr,
           actions=(Notify("pr_attention"),)),
    *_rest(_PR_POKES, Ignore("the PR is acted on only while parked on it")),
    *_rows(_LIVE, [E.PR_MERGED], K.DONE, actions=(EndSession(), FinishMerged(),
                                                 Notify("task_done"))),
    *_rows(_LIVE, [E.PR_CLOSED], K.FAILED, build=_failed(FailReason.PR_CLOSED),
           actions=(EndSession(), Notify("pr_closed"))),
    *_rest((E.PR_MERGED, E.PR_CLOSED), Ignore("the task has ended")),
])


def decide(task: TaskState | At, event: Event) -> Transition | Ignore | Reject:
    """The table's answer to `event` for `task`. Pure: no file, clock,
    network or session."""
    at = _at(task)
    facts = Facts(at.stage, at.status, event)
    got = _answer(_match(facts), facts)
    if not isinstance(got, Transition):
        return got
    got = _held_for_login(facts, got)
    return replace(got, actions=_with_slot(at.status, got.status, got.actions))


def _answer(row: Row, f: Facts) -> Transition | Ignore | Reject:
    if isinstance(row.to, Via):
        return row.to.rule(f)
    if not isinstance(row.to, Kind):
        return row.to
    status = row.build(f) if row.build else Status(row.to)
    return Transition(status, row.move[1] if row.move else f.stage,
                      row.actions + tuple(d(f) for d in row.derive))


_LOGIN_HOLDS = (Kind.PARKED_HUMAN, Kind.PARKED_ANSWERS)


def _held_for_login(f: Facts, t: Transition) -> Transition:
    """A park of a live session at a login prompt holds it there instead:
    the pane is where the code is typed. Its round is still counted."""
    if not f.event.login or t.status.kind not in _LOGIN_HOLDS:
        return t
    kept = tuple(a for a in t.actions if isinstance(a, ApplyRound))
    return Transition(Status(Kind.HELD_LOGIN, note=t.status.note), t.stage,
                      (*kept, ClearTurnMarkers(), Notify("needs_relogin", t.status.note)))


def _match(f: Facts) -> Row:
    return next(r for r in TABLE if r.decides(f))


def _with_slot(before: Status, after: Status,
               actions: tuple[Action, ...]) -> tuple[Action, ...]:
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
    return [e for r in rows for e in _edges(r)]


def graph() -> dict:
    """Every transition row as an edge between status kinds, and every stage
    move as an edge between stages."""
    rows = [r for r in TABLE if isinstance(r.to, (Kind, Via))]
    return {"status": {"nodes": [k.value for k in Kind], "edges": _status_edges(rows)},
            "stage": {"nodes": [s.value for s in PIPELINE], "edges": _stage_edges(rows)}}


def _status_edges(rows: list[Row]) -> list[Edge]:
    return [e for r in rows for e in _edges(r)]


def _stage_edges(rows: list[Row]) -> list[Edge]:
    return [Edge(r.move[0].value, r.event.value, r.guard.__name__, r.move[1].value)
            for r in rows if r.move]


def _edges(r: Row) -> list[Edge]:
    """A transition row's edge; one per status a rule row can enter."""
    if isinstance(r.to, Via):
        return [Edge(r.source.value, r.event.value, r.to.value, k.value) for k in r.to.kinds]
    if isinstance(r.to, Kind):
        return [Edge(r.source.value, r.event.value, r.guard.__name__, r.to.value)]
    return []
