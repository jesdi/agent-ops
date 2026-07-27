"""Pure per-task state machine: (state, stage signal, tmux liveness) → actions.

queued → spec → awaiting-spec-review → plan → implement → pr-open
                                    ↘ blocked (any stage) ↗
                                    ↘ failed / stalled-on-budget
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dispatcher.artifacts import CheckResult, check_plan, check_spec
from dispatcher.state import IN_FLIGHT_STAGES, Stage, StageSignal, TaskState


@dataclass(frozen=True)
class SpawnStage:
    stage: Stage


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


@dataclass(frozen=True)
class HandleCrash:
    pass


@dataclass(frozen=True)
class NoOp:
    pass


@dataclass(frozen=True)
class ParkForInput:
    note: str = ""


@dataclass(frozen=True)
class ParkForCI:
    run_id: int


NEXT_STAGE = {
    Stage.SPEC: Stage.PLAN,
    Stage.AWAITING_SPEC_REVIEW: Stage.PLAN,
    Stage.PLAN: Stage.IMPLEMENT,
    Stage.IMPLEMENT: Stage.PR_OPEN,
}

_CHECKERS = {
    Stage.SPEC: check_spec,
    Stage.AWAITING_SPEC_REVIEW: check_spec,
    Stage.PLAN: check_plan,
}

# A plan that fails the mechanical format check is usually a heading-level
# slip, not a bad plan — resume the session with the reason this many times
# before giving up and failing the task.
PLAN_RETRY_LIMIT = 1


def _artifact_path(task: TaskState, signal: StageSignal) -> Path:
    p = Path(signal.artifact)
    return p if p.is_absolute() else Path(task.worktree) / p


def next_actions(
    task: TaskState,
    signal: StageSignal | None,
    session_alive: bool,
    waiting: bool = False,
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

    if signal is None:
        return [NoOp()]

    if signal.status == "awaiting-ci":
        if signal.run_id <= 0:
            return [SetTaskStage(Stage.FAILED),
                    Notify("artifact_failed", "awaiting-ci without run_id")]
        return [ParkForCI(signal.run_id)]

    if signal.status == "blocked":
        if task.stage == Stage.BLOCKED:
            return [NoOp()]  # legacy escalate-in-place state
        return [ParkForInput(signal.note)]

    if signal.status == "working":
        if waiting and session_alive:
            return [ParkForInput("(session stopped mid-stage waiting for input)")]
        return [NoOp()]

    if signal.status == "awaiting-review":
        if task.stage == Stage.AWAITING_SPEC_REVIEW:
            return [NoOp()]  # already notified on a previous pass
        if task.stage != Stage.SPEC:
            return [NoOp()]  # only the SPEC stage emits awaiting-review; ignore stale/misrouted
        return [SetTaskStage(Stage.AWAITING_SPEC_REVIEW),
                Notify("awaiting_spec_review", signal.note)]

    if done:
        if task.stage == Stage.IMPLEMENT:
            return [SetTaskStage(Stage.PR_OPEN), Notify("pr_opened", signal.note)]
        checker = _CHECKERS.get(task.stage)
        if checker is not None:
            result: CheckResult = checker(_artifact_path(task, signal))
            if not result.ok:
                if (task.stage == Stage.PLAN
                        and task.plan_retries < PLAN_RETRY_LIMIT):
                    return [RetryStage(Stage.PLAN, result.reason)]
                return [SetTaskStage(Stage.FAILED),
                        Notify("artifact_failed", result.reason)]
        nxt = NEXT_STAGE.get(task.stage)
        if nxt is None:
            return [NoOp()]   # done signal for a terminal/unknown stage — ignore
        acts: list[object] = [SpawnStage(nxt)]
        if nxt == Stage.IMPLEMENT:
            # No plan gate — the ping links the plan so a human can kill
            # a bad run early from the phone.
            acts.append(Notify("implement_started", signal.artifact))
        return acts

    return [NoOp()]
