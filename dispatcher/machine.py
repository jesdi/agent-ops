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

from dispatcher.artifacts import TICKETS_DIR, CheckResult, check_spec, check_tickets
from dispatcher.state import IN_FLIGHT_STAGES, LoopCaps, Stage, StageSignal, TaskState


@dataclass(frozen=True)
class SpawnStage:
    stage: Stage
    ticket: int = 0   # implement only: the 1-based ticket this session works


@dataclass(frozen=True)
class SetTickets:
    """Move the ticket cursor: `cursor` is the ticket about to be implemented,
    `count` the size of the set. Emitted before the SpawnStage it feeds."""
    cursor: int
    count: int


@dataclass(frozen=True)
class RecordRound:
    """A bounded loop reported a round the task has not counted yet; the
    executor bumps the counter and writes the `round` event."""
    loop: str
    round: int


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


@dataclass(frozen=True)
class ParkForCI:
    run_id: int


@dataclass(frozen=True)
class ParkForReview:
    """Grace expired at the spec-review gate. Unlike every other park this
    one releases the E2E slot too, so the dispatcher can spend it on the next
    Ready task instead of holding it for a human who is asleep."""


# Loop name in a session's signal → the TaskState counter that owns it.
LOOP_FIELDS = {"review": "review_rounds", "gate": "gate_rounds",
               "e2e": "e2e_rounds", "ci": "ci_rounds"}

# A ticket set that fails the mechanical check is usually a numbering or
# heading slip — resume the session with the reason this many times before
# giving up and failing the task.
PLAN_RETRY_LIMIT = 1


def _artifact_path(task: TaskState, signal: StageSignal) -> Path:
    p = Path(signal.artifact)
    return p if p.is_absolute() else Path(task.worktree) / p


def _loop_actions(task: TaskState, signal: StageSignal,
                  caps: LoopCaps) -> list[object]:
    """Round bookkeeping for a `working` signal that names a loop. Only a
    round ABOVE the task's counter counts: a resumed session re-reporting
    an old round changes nothing, so the budget can never be reset from
    inside a session."""
    field = LOOP_FIELDS.get(signal.loop)
    if field is None or signal.status != "working":
        return []
    have = getattr(task, field)
    cap = getattr(caps, signal.loop)
    if signal.round <= have:
        return []
    acts: list[object] = [RecordRound(signal.loop, signal.round)]
    if signal.round > cap:
        acts.append(ParkForInput(f"{signal.loop} loop exceeded its cap of {cap} rounds"))
    elif signal.round == cap:
        acts.append(Notify("last_round", f"{signal.loop} round {cap}/{cap}"))
    return acts


def next_actions(
    task: TaskState,
    signal: StageSignal | None,
    session_alive: bool,
    waiting: bool = False,
    idle_seconds: float | None = None,
    stall_after: float = 600.0,
    grace_elapsed: bool = False,
    caps: LoopCaps = LoopCaps(),
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
    if loop_acts and isinstance(loop_acts[-1], ParkForInput):
        return loop_acts

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
        return [ParkForInput(signal.note, artifact=signal.artifact)]

    if signal.status == "working":
        if waiting and session_alive:
            return loop_acts + [ParkForInput("(session stopped mid-stage waiting for input)")]
        return loop_acts or [NoOp()]

    if signal.status == "awaiting-review":
        if task.stage == Stage.AWAITING_SPEC_REVIEW:
            if grace_elapsed:
                return [ParkForReview()]
            return [NoOp()]  # already notified on a previous pass
        if task.stage != Stage.SPEC:
            return [NoOp()]  # only the SPEC stage emits awaiting-review
        return [SetTaskStage(Stage.AWAITING_SPEC_REVIEW, artifact=signal.artifact),
                PublishSpec(artifact=signal.artifact),
                Notify("awaiting_spec_review", signal.note)]

    if done:
        if task.stage == Stage.IMPLEMENT:
            if task.ticket_cursor < task.ticket_count:
                nxt = task.ticket_cursor + 1
                return [SetTickets(nxt, task.ticket_count),
                        SpawnStage(Stage.IMPLEMENT, ticket=nxt)]
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
            return [SetTickets(1, result.count), SpawnStage(Stage.IMPLEMENT, ticket=1),
                    Notify("implement_started", f"{result.count} ticket(s)")]
        if task.stage in (Stage.SPEC, Stage.AWAITING_SPEC_REVIEW):
            result = check_spec(_artifact_path(task, signal))
            if not result.ok:
                return [SetTaskStage(Stage.FAILED), Notify("artifact_failed", result.reason)]
            return [SpawnStage(Stage.PLAN)]
        return [NoOp()]   # done signal for a terminal/unknown stage — ignore

    return [NoOp()]
