"""Pure per-task state machine: (state, stage signal, session liveness) → actions.

queued → spec → plan → awaiting-plan-review → implement ×tickets → review → pr-open
                                    ↘ blocked/awaiting-answers (any stage) ↗   ⇅
                                    ↘ failed / stalled-on-budget
                       pr-open ⇄ address-review, pr-open → done|failed
Every bounded loop (review fixes, gate fixes, e2e, ci) parks past its cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from dispatcher.artifacts import TICKETS_DIR, CheckResult, check_spec, check_tickets
from dispatcher.loops import Decision, Loop, Outcome, ReportedRound, evaluate
from dispatcher.state import (IN_FLIGHT_STAGES, BackgroundWait, LoopCaps, Stage,
                              StageSignal, TaskState)


@dataclass(frozen=True)
class SpawnStage:
    stage: Stage


@dataclass(frozen=True)
class StartTicket:
    """Atomic between-tickets IMPLEMENT start: admission check, cursor advance,
    and session spawn happen together. The executor checks budget_ok first and
    makes no state mutation when denied."""
    cursor: int
    count: int


@dataclass(frozen=True)
class ApplyDecision:
    """A loop policy decision that the executor must apply: save the counter,
    emit the round event, and (for LAST_ROUND/EXHAUSTED) notify / park."""
    decision: Decision


@dataclass(frozen=True)
class RetryStage:
    """Re-run an in-flight stage in-session (via --continue) with corrective
    feedback, instead of failing the task outright. Used when an artifact
    fails its mechanical format check but the content is likely salvageable.
    `slip` marks a plan signal that broke the protocol (an unapproved `done`,
    an unknown track): it spends its own retry, not the ticket check's."""
    stage: Stage
    reason: str = ""
    slip: bool = False


@dataclass(frozen=True)
class Notify:
    template: str
    note: str = ""


@dataclass(frozen=True)
class SetTaskStage:
    stage: Stage
    artifact: str = ""


@dataclass(frozen=True)
class PublishSpec:
    """Deterministic backstop at the plan-review gate: the executor makes
    sure the task's spec folder is committed+pushed and surfaces the GitHub
    link (or a local-only warning) in the review ping. Emitted between
    SetTaskStage and Notify so the publish outcome can shape the notification."""


@dataclass(frozen=True)
class HandleCrash:
    pass


@dataclass(frozen=True)
class NoOp:
    pass


@dataclass(frozen=True)
class ParkForInput:
    note: str = ""
    artifact: str = ""   # awaiting-answers: the file the operator must answer
    is_answers: bool = False  # True only for awaiting-answers signals


@dataclass(frozen=True)
class ParkForCI:
    run_id: int


@dataclass(frozen=True)
class DisarmPlanApproval:
    """The session at the gate reports `working`: it reworks the plan on the
    operator's feedback, so the summary the console offers for approval is
    stale. Its next ready report is a new review round."""


@dataclass(frozen=True)
class ParkForReview:
    """Grace expired at the plan-review gate. Unlike every other park this
    one releases the E2E slot too, so the dispatcher can spend it on the next
    Ready task instead of holding it for a human who is asleep. `artifact`
    is the summary the park arms when no request is armed."""
    artifact: str = ""


@dataclass(frozen=True)
class RecordBackgroundWait:
    """Note on the task that this background report was seen with herdr's
    state-change counter at `seq`; the wait is in force from now on."""
    reported: float
    seq: int


class BackgroundView(NamedTuple):
    """What a pass knows about a task's background wait (an input, not an
    action — hence no dataclass)."""
    wait: BackgroundWait
    agent: tuple[str, int] | None   # herdr (status, state-change counter); None = unknown
    now: float
    cap: int                        # background_wait_seconds


# A ticket set that fails the mechanical check is usually a numbering or
# heading slip — resume the session with the reason this many times before
# giving up and failing the task. A plan `done` that skipped the gate or names
# no configured track is a protocol slip: it has a retry of its own
# (TaskState.plan_slips), then parks. Gate entry resets both.
PLAN_RETRY_LIMIT = 1
PLAN_NO_APPROVAL = ('status "done" is accepted only after the operator has '
                    'approved the plan at the review gate; write the review '
                    'summary and report status "awaiting-review" with the '
                    'summary path as "artifact", then wait for the reply')
# A spec signal that names no configured track, or asks for a review this
# stage no longer has, is a protocol slip, not a judgment: resume the session
# once with the reason, then park for the operator.
SPEC_RETRY_LIMIT = 1
SPEC_NO_REVIEW = ('the spec stage has no review gate and status '
                  '"awaiting-review" is not valid in it; once stage 1 is '
                  'committed and pushed, report status "done" with the '
                  'spec.md path as "artifact" and a "track"')


def _artifact_path(task: TaskState, signal: StageSignal) -> Path:
    p = Path(signal.artifact)
    return p if p.is_absolute() else Path(task.worktree) / p


def _loop_actions(task: TaskState, signal: StageSignal,
                  caps: LoopCaps) -> list[object]:
    """Round bookkeeping for a `working` signal that names a loop. Routes
    through the pure policy; only signals with a round above the stored
    counter produce an action. UNCHANGED → empty list (no-op)."""
    if signal.status != "working":
        return []
    try:
        loop = Loop(signal.loop)
    except ValueError:
        return []
    decision = evaluate(task, ReportedRound(loop, signal.round), caps)
    if decision.outcome is Outcome.UNCHANGED:
        return []
    return [ApplyDecision(decision)]


def _track_actions(task: TaskState, signal: StageSignal,
                   tracks: frozenset[str] | None) -> list[object]:
    """Empty when the signal's track is valid (or validation is off). A plan
    approval may leave the track out: the task keeps the one it has."""
    at_gate = task.stage == Stage.AWAITING_PLAN_REVIEW
    if tracks is None or signal.track in tracks or (at_gate and not signal.track):
        return []
    reason = (f"stage.json names track {signal.track!r}; it must be one of "
              f"{sorted(tracks)}")
    return _plan_bounce(task, reason) if at_gate else _spec_bounce(task, reason)


def _spec_bounce(task: TaskState, reason: str) -> list[object]:
    if task.spec_retries < SPEC_RETRY_LIMIT:
        return [RetryStage(Stage.SPEC, reason)]
    return [ParkForInput(reason)]


def _plan_bounce(task: TaskState, reason: str) -> list[object]:
    if task.plan_slips < PLAN_RETRY_LIMIT:
        return [RetryStage(Stage.PLAN, reason, slip=True)]
    return [ParkForInput(reason)]


def _dead_session_actions(task: TaskState) -> list[object]:
    """Empty when a dead session needs no action of its own."""
    if task.stage == Stage.AWAITING_PLAN_REVIEW:
        # Reboot recovery: gate-parked tasks don't expire — re-spawn a
        # fresh plan session; the spec folder is on disk in the worktree.
        return [SpawnStage(Stage.PLAN)]
    if task.stage in IN_FLIGHT_STAGES:
        return [HandleCrash()]
    return []


def _stalled(signal: StageSignal | None, session_alive: bool, waiting: bool,
             idle_seconds: float | None, stall_after: float) -> bool:
    # Time-based liveness (unchanged): gated statuses are idle by design and
    # fall through to their own rules.
    stalled = (session_alive and stall_after > 0
               and idle_seconds is not None and idle_seconds > stall_after)
    return stalled and (signal is None
                        or (signal.status == "working" and not waiting))


def _ci_actions(signal: StageSignal) -> list[object]:
    if signal.run_id <= 0:
        return [SetTaskStage(Stage.FAILED),
                Notify("artifact_failed", "awaiting-ci without run_id")]
    return [ParkForCI(signal.run_id)]


def _blocked_actions(task: TaskState, signal: StageSignal) -> list[object]:
    if task.stage == Stage.BLOCKED:
        return [NoOp()]  # legacy escalate-in-place state
    return [ParkForInput(signal.note)]


def _working_actions(task: TaskState, signal: StageSignal, session_alive: bool,
                     waiting: bool, caps: LoopCaps) -> list[object]:
    loop_acts = _loop_actions(task, signal, caps)
    if task.stage == Stage.AWAITING_PLAN_REVIEW and task.operator_request:
        loop_acts = [DisarmPlanApproval()] + loop_acts
    if waiting and session_alive:
        return loop_acts + [ParkForInput("(session stopped mid-stage waiting for input)")]
    return loop_acts or [NoOp()]


def _review_actions(task: TaskState, signal: StageSignal,
                    grace_elapsed: bool) -> list[object]:
    if task.stage == Stage.AWAITING_PLAN_REVIEW:
        if grace_elapsed:
            return [ParkForReview(artifact=signal.artifact)]
        if task.operator_request:
            return [NoOp()]  # already notified on a previous pass
        # No request armed: a resume or a rework cleared it, so this ready
        # report is a new review round. It enters the gate again.
    elif task.stage == Stage.SPEC:
        # The spec stage has no review gate: nobody would answer this.
        return _spec_bounce(task, SPEC_NO_REVIEW)
    elif task.stage != Stage.PLAN:
        return [NoOp()]
    result, failed = _checked_tickets(task)
    return failed or [
        SetTaskStage(Stage.AWAITING_PLAN_REVIEW, artifact=signal.artifact),
        PublishSpec(),
        Notify("awaiting_plan_review", signal.note)]


def _checked_tickets(task: TaskState) -> tuple[CheckResult, list[object]]:
    """The ticket set's check, and the actions when it fails (else empty)."""
    result = check_tickets(Path(task.worktree) / TICKETS_DIR)
    if result.ok:
        return result, []
    if task.plan_retries < PLAN_RETRY_LIMIT:
        return result, [RetryStage(Stage.PLAN, result.reason)]
    return result, [SetTaskStage(Stage.FAILED),
                    Notify("artifact_failed", result.reason)]


def _plan_done_actions(task: TaskState, signal: StageSignal,
                       tracks: frozenset[str] | None) -> list[object]:
    """The operator's approval, reported by the plan session at the gate. A
    configured track it names is already on the task (main._adopt_track)."""
    bounced = _track_actions(task, signal, tracks)
    if bounced:
        return bounced
    result, failed = _checked_tickets(task)
    return failed or [StartTicket(1, result.count),
                      Notify("implement_started", f"{result.count} ticket(s)")]


def _spec_done_actions(task: TaskState, signal: StageSignal,
                       tracks: frozenset[str] | None) -> list[object]:
    bounced = _track_actions(task, signal, tracks)
    if bounced:
        return bounced
    result = check_spec(_artifact_path(task, signal))
    if not result.ok:
        return [SetTaskStage(Stage.FAILED), Notify("artifact_failed", result.reason)]
    return [SpawnStage(Stage.PLAN)]


def _done_actions(task: TaskState, signal: StageSignal,
                  tracks: frozenset[str] | None) -> list[object]:
    if task.stage == Stage.IMPLEMENT:
        if task.ticket_cursor < task.ticket_count:
            nxt = task.ticket_cursor + 1
            return [StartTicket(nxt, task.ticket_count)]
        return [SpawnStage(Stage.REVIEW), Notify("review_started", signal.note)]
    if task.stage == Stage.REVIEW:
        return [SetTaskStage(Stage.PR_OPEN), Notify("pr_opened", signal.note)]
    if task.stage == Stage.ADDRESS_REVIEW:
        # The PR is the artifact — nothing to format-check.
        return [SetTaskStage(Stage.PR_OPEN), Notify("pr_updated", signal.note)]
    if task.stage == Stage.PLAN:
        # Nobody approved this plan: the gate comes first.
        return _plan_bounce(task, PLAN_NO_APPROVAL)
    if task.stage == Stage.AWAITING_PLAN_REVIEW:
        return _plan_done_actions(task, signal, tracks)
    if task.stage == Stage.SPEC:
        return _spec_done_actions(task, signal, tracks)
    return [NoOp()]   # done signal for a terminal/unknown stage — ignore


def next_actions(
    task: TaskState,
    signal: StageSignal | None,
    session_alive: bool,
    waiting: bool = False,
    idle_seconds: float | None = None,
    stall_after: float = 600.0,
    grace_elapsed: bool = False,
    caps: LoopCaps = LoopCaps(),
    tracks: frozenset[str] | None = None,
) -> list[object]:
    if task.park:
        return [NoOp()]  # wake/resume is dispatcher-side; never re-park

    done = signal is not None and signal.status == "done"

    if not session_alive and not done:
        dead = _dead_session_actions(task)
        if dead:
            return dead

    if _stalled(signal, session_alive, waiting, idle_seconds, stall_after):
        return [ParkForInput(
            f"(no session output for {int(stall_after) // 60}m — "
            f"likely login/trust prompt or hang)")]

    if signal is None:
        return [NoOp()]

    if signal.status == "awaiting-ci":
        return _ci_actions(signal)

    if signal.status == "blocked":
        return _blocked_actions(task, signal)

    if signal.status == "awaiting-answers":
        # Questionnaire, prototype or wizard: an input park that carries the
        # file the operator must look at. The console serves it.
        return [ParkForInput(signal.note, artifact=signal.artifact, is_answers=True)]

    if signal.status == "working":
        return _working_actions(task, signal, session_alive, waiting, caps)

    if signal.status == "awaiting-review":
        return _review_actions(task, signal, grace_elapsed)

    if done:
        return _done_actions(task, signal, tracks)

    return [NoOp()]


def _in_wait(signal: StageSignal | None, session_alive: bool, waiting: bool,
             view: BackgroundView | None) -> bool:
    """A background marker decides this pass only for a live session whose
    stage signal says `working`; a waiting marker wins. (Parked tasks are
    never driven, so park needs no check here.)"""
    return (view is not None and session_alive and not waiting
            and signal is not None and signal.status == "working")


def _wait_actions(task: TaskState, view: BackgroundView) -> list[object] | None:
    """The background wait's actions this pass; None while herdr's counter
    differs from the recorded one: the session started a new turn and the
    wait is over until the next report. The marker stays, so a later report
    of the same work keeps its cap clock."""
    if view.agent is None:
        return []   # herdr cannot be asked: hold
    status, seq = view.agent
    if view.wait.reported != task.background_reported:
        return [] if status == "working" else [RecordBackgroundWait(view.wait.reported, seq)]
    if seq != task.background_seq:
        return None
    if view.now - view.wait.since > view.cap:
        return [ParkForInput(f"(background work still running after "
                             f"{view.cap // 60}m — cap reached)")]
    return []


def pass_actions(
    task: TaskState,
    signal: StageSignal | None,
    session_alive: bool,
    waiting: bool,
    view: BackgroundView | None,
    idle_seconds: float | None = None,
    stall_after: float = 600.0,
    grace_elapsed: bool = False,
    caps: LoopCaps = LoopCaps(),
    tracks: frozenset[str] | None = None,
) -> list[object]:
    """next_actions, deferring to a background wait (design "Data model"):
    while the wait holds, neither the stall timer nor a park applies, bar
    the cap; once it is over today's rules decide."""
    acts = _wait_actions(task, view) if _in_wait(signal, session_alive, waiting, view) else None
    if acts is not None:
        return (_loop_actions(task, signal, caps) + acts) or [NoOp()]
    return next_actions(task, signal, session_alive, waiting=waiting,
                        idle_seconds=idle_seconds, stall_after=stall_after,
                        grace_elapsed=grace_elapsed, caps=caps,
                        tracks=tracks)
