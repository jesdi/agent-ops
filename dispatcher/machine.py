"""Pure per-task state machine: (state, stage signal, session liveness) → actions.

queued → spec → awaiting-spec-review → plan → implement ×tickets → review → pr-open
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
    fails its mechanical format check but the content is likely salvageable."""
    stage: Stage
    reason: str = ""


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
    """Deterministic backstop at the spec-review gate: the executor makes
    sure the spec is committed+pushed and surfaces the GitHub link (or a
    local-only warning) in the review ping. Emitted between SetTaskStage
    and Notify so the publish outcome can shape the notification."""
    artifact: str = ""


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
class ArmSpecApproval:
    """Re-establish the spec-approval operator_request on a resumed gate task
    whose operator_request was cleared by the resume (slice 12). Does NOT
    re-stamp updated_at (grace clock must not restart) and does NOT emit
    SetTaskStage (no stage transition, no re-publish, no re-notify)."""
    artifact: str = ""


@dataclass(frozen=True)
class ParkForReview:
    """Grace expired at the spec-review gate. Unlike every other park this
    one releases the E2E slot too, so the dispatcher can spend it on the next
    Ready task instead of holding it for a human who is asleep."""


@dataclass(frozen=True)
class RecordBackgroundWait:
    """Note on the task that this background report was seen with herdr's
    state-change counter at `seq`; the wait is in force from now on."""
    reported: float
    seq: int


@dataclass(frozen=True)
class EndBackgroundWait:
    """The session started a new turn: delete the background marker."""


class BackgroundView(NamedTuple):
    """What a pass knows about a task's background wait (an input, not an
    action — hence no dataclass)."""
    wait: BackgroundWait
    agent: tuple[str, int] | None   # herdr (status, state-change counter); None = unknown
    now: float
    cap: int                        # background_wait_seconds


# A ticket set that fails the mechanical check is usually a numbering or
# heading slip — resume the session with the reason this many times before
# giving up and failing the task.
PLAN_RETRY_LIMIT = 1
# A spec signal that names no configured track is a forgotten field, not a
# judgment: resume the session once with the list, then park for the operator.
SPEC_RETRY_LIMIT = 1


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
    """Empty when the spec signal's track is valid (or validation is off)."""
    if tracks is None or signal.track in tracks:
        return []
    reason = (f"stage.json names track {signal.track!r}; it must be one of "
              f"{sorted(tracks)}")
    if task.spec_retries < SPEC_RETRY_LIMIT:
        return [RetryStage(Stage.SPEC, reason)]
    return [ParkForInput(reason)]


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
        if task.stage == Stage.AWAITING_SPEC_REVIEW:
            # Reboot recovery: gate-parked tasks don't expire — re-spawn a
            # fresh spec session; the draft spec is on disk in the worktree.
            return [SpawnStage(Stage.SPEC)]
        if task.stage in IN_FLIGHT_STAGES:
            return [HandleCrash()]

    # Time-based liveness (unchanged): gated statuses are idle by design and
    # fall through to their own rules below.
    stalled = (session_alive and stall_after > 0
               and idle_seconds is not None and idle_seconds > stall_after)
    if stalled and (signal is None
                    or (signal.status == "working" and not waiting)):
        return [ParkForInput(
            f"(no session output for {int(stall_after) // 60}m — "
            f"likely login/trust prompt or hang)")]

    if signal is None:
        return [NoOp()]

    loop_acts = _loop_actions(task, signal, caps)

    if signal.status == "awaiting-ci":
        if signal.run_id <= 0:
            return [SetTaskStage(Stage.FAILED),
                    Notify("artifact_failed", "awaiting-ci without run_id")]
        return [ParkForCI(signal.run_id)]

    if signal.status == "blocked":
        if task.stage == Stage.BLOCKED:
            return [NoOp()]  # legacy escalate-in-place state
        return [ParkForInput(signal.note)]

    if signal.status == "awaiting-answers":
        # Questionnaire, prototype or wizard: an input park that carries the
        # file the operator must look at. The console serves it.
        return [ParkForInput(signal.note, artifact=signal.artifact, is_answers=True)]

    if signal.status == "working":
        if waiting and session_alive:
            return loop_acts + [ParkForInput("(session stopped mid-stage waiting for input)")]
        return loop_acts or [NoOp()]

    if signal.status == "awaiting-review":
        if task.stage == Stage.AWAITING_SPEC_REVIEW:
            if grace_elapsed:
                return [ParkForReview()]
            if not task.operator_request:
                return [ArmSpecApproval(artifact=signal.artifact)]
            return [NoOp()]  # already notified on a previous pass
        if task.stage != Stage.SPEC:
            return [NoOp()]  # only the SPEC stage emits awaiting-review
        return _track_actions(task, signal, tracks) or [
            SetTaskStage(Stage.AWAITING_SPEC_REVIEW, artifact=signal.artifact),
            PublishSpec(artifact=signal.artifact),
            Notify("awaiting_spec_review", signal.note)]

    if done:
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
            result: CheckResult = check_tickets(Path(task.worktree) / TICKETS_DIR)
            if not result.ok:
                if task.plan_retries < PLAN_RETRY_LIMIT:
                    return [RetryStage(Stage.PLAN, result.reason)]
                return [SetTaskStage(Stage.FAILED), Notify("artifact_failed", result.reason)]
            return [StartTicket(1, result.count),
                    Notify("implement_started", f"{result.count} ticket(s)")]
        if task.stage in (Stage.SPEC, Stage.AWAITING_SPEC_REVIEW):
            bounced = _track_actions(task, signal, tracks)
            if bounced:
                return bounced
            result = check_spec(_artifact_path(task, signal))
            if not result.ok:
                return [SetTaskStage(Stage.FAILED), Notify("artifact_failed", result.reason)]
            return [SpawnStage(Stage.PLAN)]
        return [NoOp()]   # done signal for a terminal/unknown stage — ignore

    return [NoOp()]


def _in_wait(task: TaskState, signal: StageSignal | None, session_alive: bool,
             waiting: bool, view: BackgroundView | None) -> bool:
    """A background marker decides this pass only for a live, unparked
    session whose stage signal says `working`; a waiting marker wins."""
    return (view is not None and session_alive and not waiting and not task.park
            and signal is not None and signal.status == "working")


def _wait_actions(task: TaskState, view: BackgroundView) -> list[object] | None:
    """The background wait's actions this pass; None once herdr's counter
    shows the session started a new turn (the wait is over)."""
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


def pass_actions(task: TaskState, signal: StageSignal | None, session_alive: bool,
                 waiting: bool, view: BackgroundView | None,
                 caps: LoopCaps = LoopCaps(), **today) -> list[object]:
    """next_actions, deferring to a background wait (design "Data model"):
    while the wait holds, neither the stall timer nor a park applies, bar
    the cap; once it is over the marker goes and today's rules decide."""
    in_wait = _in_wait(task, signal, session_alive, waiting, view)
    acts = _wait_actions(task, view) if in_wait else None
    if acts is not None:
        return (_loop_actions(task, signal, caps) + acts) or [NoOp()]
    lead: list[object] = [EndBackgroundWait()] if in_wait else []
    return lead + next_actions(task, signal, session_alive, waiting=waiting,
                               caps=caps, **today)
