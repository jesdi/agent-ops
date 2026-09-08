"""Pure loop-budget policy: (task state, caps, observation) → decision.

Every bounded loop (review fixes, gate fixes, e2e runs, ci fixes) may run a
fixed number of rounds before the task parks for a human. This module owns the
round accounting and the cap arithmetic — nothing else. It does NO I/O and
mutates nothing on disk: the dispatcher OBSERVES a loop event, asks
`evaluate()` what it means, and puts the counter change into effect via
`apply()`. The counter-field mapping is private here; callers name a loop,
never its TaskState field.

state.py stays the serialization owner and must not import this module (loops
depends on state, not the other way round).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from dispatcher.state import LoopCaps, TaskState

# Loop name → the TaskState counter that owns it. PRIVATE: the whole point of
# the policy is that no caller outside this module needs to know which field
# backs which loop.
_FIELDS = {"review": "review_rounds", "gate": "gate_rounds",
           "e2e": "e2e_rounds", "ci": "ci_rounds"}


@dataclass(frozen=True)
class SessionRound:
    """A live session reported it is on round N of a bounded loop (from its
    stage.json signal). ABSOLUTE: N is the round the session believes it is on,
    not a delta — a resumed session re-reporting an old round must never
    advance the counter. Only a `working` signal counts."""
    loop: str
    round: int
    status: str


@dataclass(frozen=True)
class FailedRun:
    """A CI run completed with a non-success conclusion. INCREMENT: the counter
    goes up by 1 from wherever it currently sits (not an absolute report).
    The caller picks the loop name: "e2e" for implement/review stages,
    "ci" for address-review."""
    loop: str    # "e2e" or "ci"
    detail: str  # e.g. "run 123 failure"


# The observation union `evaluate` accepts.
Observation = SessionRound | FailedRun


class Outcome(Enum):
    """What an observation resolved to. Consumers ACT on this; they never
    recompute it from round and cap."""
    UNCHANGED = "unchanged"        # nothing to record: unknown loop, idle signal, or a stale/duplicate round
    WITHIN_LIMIT = "within-limit"  # counted; the cap still allows another attempt
    LAST_ROUND = "last-round"      # counted; this is the final permitted round (warn the operator)
    EXHAUSTED = "exhausted"        # over the cap; the task must park


@dataclass(frozen=True)
class Decision:
    """The outcome of one observation plus the counter update it carries.
    `round`/`cap` are the resolved counter value and its ceiling — enough for
    the `round` event and the `last_round` ping. `detail` is optional context
    appended to the event (e.g. which CI run failed); empty on the session
    path."""
    loop: str
    outcome: Outcome
    round: int = 0
    cap: int = 0
    detail: str = ""


_UNCHANGED = Decision("", Outcome.UNCHANGED)


def evaluate(task: TaskState, obs: Observation, caps: LoopCaps) -> Decision:
    """Interpret an observation against the task's current counter and caps."""
    if isinstance(obs, SessionRound):
        if obs.status != "working" or obs.loop not in _FIELDS:
            return _UNCHANGED
        have = getattr(task, _FIELDS[obs.loop])
        if obs.round <= have:
            return _UNCHANGED   # absolute: an equal/lower report never advances
        return _classify(obs.loop, obs.round, getattr(caps, obs.loop))
    if isinstance(obs, FailedRun):
        n = getattr(task, _FIELDS[obs.loop]) + 1
        return _classify(obs.loop, n, getattr(caps, obs.loop), obs.detail)
    raise TypeError(f"unsupported observation: {obs!r}")


def _classify(loop: str, n: int, cap: int, detail: str = "") -> Decision:
    """Shared threshold calc: a resolved counter value against its cap. Reused
    by every observation type — absolute (session) and increment (CI/PR)."""
    if n > cap:
        outcome = Outcome.EXHAUSTED
    elif n == cap:
        outcome = Outcome.LAST_ROUND
    else:
        outcome = Outcome.WITHIN_LIMIT
    return Decision(loop, outcome, round=n, cap=cap, detail=detail)


def apply(task: TaskState, decision: Decision) -> TaskState:
    """Put a decision's counter update into effect, leaving every OTHER field
    untouched (PR cursors a caller advanced before applying survive). A no-op
    for UNCHANGED."""
    if decision.outcome is Outcome.UNCHANGED:
        return task
    return replace(task, **{_FIELDS[decision.loop]: decision.round})
