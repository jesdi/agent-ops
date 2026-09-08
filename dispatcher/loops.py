"""Pure loop-budget policy for dispatcher task orchestration.

Design notes (not implemented here):
- Loop eligibility (is another fix attempt allowed?) and execution admission
  (which runtime has allowance / should it wait?) are SEPARATE decisions;
  a WITHIN_LIMIT outcome is eligibility to retry, NOT permission to launch.
- Reset causes describe LOGICAL work (new stage/ticket, operator intervention,
  new PR cycle) — a model switch / subscription reset is NOT by itself a fresh
  fix-loop allowance.
- This module must stay independent of model IDs, runtime/provider names,
  credentials, subscription snapshots, usage APIs.
- state.py stays the serialization owner; loops depends on state, never the
  reverse.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from dispatcher.state import LoopCaps, Stage, TaskState


class Loop(Enum):
    REVIEW = "review"
    GATE = "gate"
    E2E = "e2e"
    CI = "ci"


# Private mapping: loop → TaskState counter field name.
_LOOP_FIELD: dict[Loop, str] = {
    Loop.REVIEW: "review_rounds",
    Loop.GATE: "gate_rounds",
    Loop.E2E: "e2e_rounds",
    Loop.CI: "ci_rounds",
}


@dataclass(frozen=True)
class FailedRun:
    """A completed run with a non-success conclusion. Increments by 1.
    The policy selects the loop from the task's stage:
    ADDRESS_REVIEW → CI; any other stage → E2E."""
    detail: str


@dataclass(frozen=True)
class PRAttention:
    """A fresh red-check or conflict on an open PR. Increments the CI loop by 1."""
    detail: str


@dataclass(frozen=True)
class ReportedRound:
    """Absolute session round report. round is the round the session believes
    it is on (not a delta). Caller has already filtered to working signals and
    known loops — no status/filtering here."""
    loop: Loop
    round: int


class Outcome(Enum):
    UNCHANGED = "unchanged"
    WITHIN_LIMIT = "within_limit"
    LAST_ROUND = "last_round"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    loop: Loop
    round: int        # resolved counter value
    cap: int
    detail: str = ""

    # Private snapshot of the counter field to apply.
    _field: str = ""
    _apply_value: int = 0

    @property
    def description(self) -> str:
        """Formatted text for round/last_round events: '{loop} round {n}/{cap}'
        optionally suffixed with ': {detail}' when detail is non-empty."""
        base = f"{self.loop.value} round {self.round}/{self.cap}"
        return f"{base}: {self.detail}" if self.detail else base

    def apply_to(self, task: TaskState) -> TaskState:
        """Return a new TaskState with only this decision's counter updated.
        UNCHANGED decisions return the task unchanged. The input task is not mutated."""
        if self.outcome == Outcome.UNCHANGED:
            return task
        return replace(task, **{self._field: self._apply_value})


class ResetCause(Enum):
    STAGE_STARTED = "stage_started"
    OPERATOR_WAKE = "operator_wake"
    PR_CYCLE_STARTED = "pr_cycle_started"


# Private: which counter fields each cause zeroes.
_RESET_FIELDS: dict[ResetCause, tuple[str, ...]] = {
    ResetCause.STAGE_STARTED: ("review_rounds", "gate_rounds", "e2e_rounds"),
    ResetCause.OPERATOR_WAKE: ("review_rounds", "gate_rounds", "e2e_rounds", "ci_rounds"),
    ResetCause.PR_CYCLE_STARTED: ("ci_rounds",),
}


def reset(task: TaskState, cause: ResetCause) -> TaskState:
    """Return a new TaskState with only the counters owned by cause zeroed.
    Every other field (cursors, stage, metadata) is preserved. Input is not mutated."""
    return replace(task, **{f: 0 for f in _RESET_FIELDS[cause]})


def _threshold(n: int, cap: int) -> Outcome:
    """Apply the shared threshold rule to a resolved counter value n and cap c."""
    if cap == 0:
        return Outcome.EXHAUSTED if n > 0 else Outcome.WITHIN_LIMIT
    if n > cap:
        return Outcome.EXHAUSTED
    if n == cap:
        return Outcome.LAST_ROUND
    return Outcome.WITHIN_LIMIT


def evaluate(task: TaskState, observation: object, caps: LoopCaps) -> Decision:
    """Interpret an observation against the task's current counter and caps.
    Pure; no I/O."""
    if isinstance(observation, ReportedRound):
        loop = observation.loop
        field = _LOOP_FIELD[loop]
        stored = getattr(task, field)
        cap = getattr(caps, loop.value)
        if observation.round <= stored:
            return Decision(
                outcome=Outcome.UNCHANGED,
                loop=loop,
                round=stored,
                cap=cap,
                _field=field,
                _apply_value=stored,
            )
        n = observation.round
        outcome = _threshold(n, cap)
        return Decision(
            outcome=outcome,
            loop=loop,
            round=n,
            cap=cap,
            _field=field,
            _apply_value=n,
        )
    if isinstance(observation, FailedRun):
        loop = Loop.CI if task.stage == Stage.ADDRESS_REVIEW else Loop.E2E
        field = _LOOP_FIELD[loop]
        stored = getattr(task, field)
        cap = getattr(caps, loop.value)
        n = stored + 1
        outcome = _threshold(n, cap)
        return Decision(
            outcome=outcome,
            loop=loop,
            round=n,
            cap=cap,
            detail=observation.detail,
            _field=field,
            _apply_value=n,
        )
    if isinstance(observation, PRAttention):
        field = _LOOP_FIELD[Loop.CI]
        stored = getattr(task, field)
        cap = caps.ci
        n = stored + 1
        outcome = _threshold(n, cap)
        return Decision(
            outcome=outcome,
            loop=Loop.CI,
            round=n,
            cap=cap,
            detail=observation.detail,
            _field=field,
            _apply_value=n,
        )
    raise TypeError(f"Unknown observation type: {type(observation)!r}")
