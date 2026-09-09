"""Tests for dispatcher.loops — strict TDD, one slice at a time."""
from dataclasses import replace

from dispatcher.loops import Loop, ReportedRound, FailedRun, PRAttention, Outcome, evaluate, ResetCause, reset
from dispatcher.state import LoopCaps, Stage, TaskState

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _task(**kw) -> TaskState:
    """Minimal TaskState for testing. Only override what a slice cares about."""
    defaults = dict(
        issue=1, target="test", stage=Stage.IMPLEMENT, slot=0,
        worktree="/tmp/wt", branch="main", title="T", updated_at="2025-01-01T00:00:00Z",
    )
    defaults.update(kw)
    return TaskState(**defaults)


# ---------------------------------------------------------------------------
# Slice 1: gate counter=1, cap=2, ReportedRound(GATE, 2) → LAST_ROUND
#           apply_to sets ONLY gate_rounds to 2, preserves everything else
# ---------------------------------------------------------------------------

def test_gate_last_round_and_apply_to():
    task = _task(gate_rounds=1, ticket_cursor=7)
    caps = LoopCaps(gate=2)
    obs = ReportedRound(loop=Loop.GATE, round=2)
    dec = evaluate(task, obs, caps)

    assert dec.outcome == Outcome.LAST_ROUND
    assert dec.round == 2
    assert dec.cap == 2

    updated = dec.apply_to(task)
    assert updated.gate_rounds == 2
    # unrelated fields preserved
    assert updated.ticket_cursor == 7
    assert updated.review_rounds == 0
    assert updated.e2e_rounds == 0
    assert updated.ci_rounds == 0
    # input not mutated
    assert task.gate_rounds == 1


# ---------------------------------------------------------------------------
# Slice 2: reported gate round BELOW cap → WITHIN_LIMIT, updates counter
# ---------------------------------------------------------------------------

def test_gate_within_limit():
    task = _task(gate_rounds=0)
    caps = LoopCaps(gate=2)
    obs = ReportedRound(loop=Loop.GATE, round=1)
    dec = evaluate(task, obs, caps)

    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.round == 1
    updated = dec.apply_to(task)
    assert updated.gate_rounds == 1


# ---------------------------------------------------------------------------
# Slice 3: reported round ABOVE cap → EXHAUSTED, records the observed round
# ---------------------------------------------------------------------------

def test_gate_exhausted():
    task = _task(gate_rounds=2)
    caps = LoopCaps(gate=2)
    obs = ReportedRound(loop=Loop.GATE, round=3)
    dec = evaluate(task, obs, caps)

    assert dec.outcome == Outcome.EXHAUSTED
    assert dec.round == 3
    updated = dec.apply_to(task)
    assert updated.gate_rounds == 3


# ---------------------------------------------------------------------------
# Slice 4: re-reporting a stored round → UNCHANGED; lower report also UNCHANGED
# ---------------------------------------------------------------------------

def test_stale_round_unchanged():
    task = _task(gate_rounds=2)
    caps = LoopCaps(gate=3)

    # equal
    dec_eq = evaluate(task, ReportedRound(Loop.GATE, 2), caps)
    assert dec_eq.outcome == Outcome.UNCHANGED
    assert dec_eq.apply_to(task).gate_rounds == 2  # unchanged

    # lower
    dec_lo = evaluate(task, ReportedRound(Loop.GATE, 1), caps)
    assert dec_lo.outcome == Outcome.UNCHANGED
    assert dec_lo.apply_to(task).gate_rounds == 2  # still unchanged


# ---------------------------------------------------------------------------
# Slice 5: round jump (1 → 4) sets counter to 4, not to stored+1
# ---------------------------------------------------------------------------

def test_absolute_jump_semantics():
    task = _task(gate_rounds=1)
    caps = LoopCaps(gate=5)
    dec = evaluate(task, ReportedRound(Loop.GATE, 4), caps)

    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.round == 4
    assert dec.apply_to(task).gate_rounds == 4


# ---------------------------------------------------------------------------
# Slice 6: cap=0 → first positive round is EXHAUSTED, never LAST_ROUND
# ---------------------------------------------------------------------------

def test_zero_cap_first_round_exhausted():
    task = _task(gate_rounds=0)
    caps = LoopCaps(gate=0)
    dec = evaluate(task, ReportedRound(Loop.GATE, 1), caps)

    assert dec.outcome == Outcome.EXHAUSTED
    assert dec.outcome != Outcome.LAST_ROUND


# ---------------------------------------------------------------------------
# Slice 7: REVIEW/E2E/CI loops use their own caps and preserve other counters
# ---------------------------------------------------------------------------

def test_review_loop_uses_review_cap():
    task = _task(review_rounds=1, gate_rounds=1, ci_rounds=1)
    caps = LoopCaps(review=2, gate=5, ci=5)
    dec = evaluate(task, ReportedRound(Loop.REVIEW, 2), caps)

    assert dec.outcome == Outcome.LAST_ROUND
    updated = dec.apply_to(task)
    assert updated.review_rounds == 2
    assert updated.gate_rounds == 1   # preserved
    assert updated.ci_rounds == 1     # preserved


def test_e2e_loop():
    task = _task(e2e_rounds=0)
    caps = LoopCaps(e2e=3)
    dec = evaluate(task, ReportedRound(Loop.E2E, 1), caps)

    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.apply_to(task).e2e_rounds == 1


def test_ci_loop():
    task = _task(ci_rounds=0)
    caps = LoopCaps(ci=3)
    dec = evaluate(task, ReportedRound(Loop.CI, 1), caps)

    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.apply_to(task).ci_rounds == 1


# ---------------------------------------------------------------------------
# Slice 8: apply_to merges owned counter only; newer PR cursors are preserved
# ---------------------------------------------------------------------------

def test_apply_to_preserves_pr_cursor():
    """Decision was evaluated against an old snapshot; the task's PR cursor
    advanced since. apply_to must not stomp the new cursor value."""
    task_old = _task(gate_rounds=0, check_cursor="2025-01-01T00:00:00Z")
    caps = LoopCaps(gate=3)
    dec = evaluate(task_old, ReportedRound(Loop.GATE, 1), caps)

    # Simulate the task having a newer cursor by the time apply_to is called.
    task_newer = replace(task_old, check_cursor="2025-06-01T00:00:00Z")
    updated = dec.apply_to(task_newer)

    assert updated.gate_rounds == 1
    assert updated.check_cursor == "2025-06-01T00:00:00Z"  # preserved


# ---------------------------------------------------------------------------
# Slice 9: Decision carries formatted description text for round/last_round events
# ---------------------------------------------------------------------------

def test_decision_description_text():
    # WITHIN_LIMIT: "gate round 1/3"
    task = _task(gate_rounds=0)
    dec = evaluate(task, ReportedRound(Loop.GATE, 1), LoopCaps(gate=3))
    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.description == "gate round 1/3"

    # LAST_ROUND: "gate round 2/2"
    task2 = _task(gate_rounds=1)
    dec2 = evaluate(task2, ReportedRound(Loop.GATE, 2), LoopCaps(gate=2))
    assert dec2.outcome == Outcome.LAST_ROUND
    assert dec2.description == "gate round 2/2"


# ---------------------------------------------------------------------------
# Slice 10: FailedRun in REVIEW stage → increments E2E loop
# ---------------------------------------------------------------------------

def test_failed_run_review_stage_increments_e2e():
    task = _task(stage=Stage.REVIEW, e2e_rounds=1)
    caps = LoopCaps(e2e=3)
    obs = FailedRun(detail="tests blew up")
    dec = evaluate(task, obs, caps)

    assert dec.loop == Loop.E2E
    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.round == 2
    assert dec.description == "e2e round 2/3: tests blew up"
    updated = dec.apply_to(task)
    assert updated.e2e_rounds == 2
    assert updated.ci_rounds == 0   # ci untouched


# ---------------------------------------------------------------------------
# Slice 11: FailedRun in ADDRESS_REVIEW stage → increments CI loop
# ---------------------------------------------------------------------------

def test_failed_run_address_review_increments_ci():
    task = _task(stage=Stage.ADDRESS_REVIEW, ci_rounds=0)
    caps = LoopCaps(ci=3)
    dec = evaluate(task, FailedRun(detail="ci red"), caps)

    assert dec.loop == Loop.CI
    assert dec.outcome == Outcome.WITHIN_LIMIT
    assert dec.round == 1
    updated = dec.apply_to(task)
    assert updated.ci_rounds == 1
    assert updated.e2e_rounds == 0  # e2e untouched


# ---------------------------------------------------------------------------
# Slice 12: PRAttention → increments CI loop
# ---------------------------------------------------------------------------

def test_pr_attention_increments_ci():
    task = _task(stage=Stage.PR_OPEN, ci_rounds=2)
    caps = LoopCaps(ci=3)
    dec = evaluate(task, PRAttention(detail="check failed"), caps)

    assert dec.loop == Loop.CI
    assert dec.outcome == Outcome.LAST_ROUND
    assert dec.round == 3
    assert dec.description == "ci round 3/3: check failed"
    updated = dec.apply_to(task)
    assert updated.ci_rounds == 3
    assert updated.e2e_rounds == 0


# ---------------------------------------------------------------------------
# Slice 13: STAGE_STARTED reset clears review/gate/e2e, RETAINS ci + metadata
# ---------------------------------------------------------------------------

def test_reset_stage_started():
    task = _task(
        review_rounds=2, gate_rounds=1, e2e_rounds=3, ci_rounds=2,
        ticket_cursor=5, check_cursor="2025-01-01T00:00:00Z",
    )
    result = reset(task, ResetCause.STAGE_STARTED)

    assert result.review_rounds == 0
    assert result.gate_rounds == 0
    assert result.e2e_rounds == 0
    assert result.ci_rounds == 2        # retained
    assert result.ticket_cursor == 5    # metadata preserved
    assert result.check_cursor == "2025-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Slice 14: OPERATOR_WAKE clears ALL FOUR counters, preserves unrelated fields
# ---------------------------------------------------------------------------

def test_reset_operator_wake():
    task = _task(
        review_rounds=2, gate_rounds=1, e2e_rounds=3, ci_rounds=2,
        ticket_cursor=5,
    )
    result = reset(task, ResetCause.OPERATOR_WAKE)

    assert result.review_rounds == 0
    assert result.gate_rounds == 0
    assert result.e2e_rounds == 0
    assert result.ci_rounds == 0
    assert result.ticket_cursor == 5   # preserved


# ---------------------------------------------------------------------------
# Slice 15: PR_CYCLE_STARTED reset clears ONLY ci
# ---------------------------------------------------------------------------

def test_reset_pr_cycle_started():
    task = _task(
        review_rounds=2, gate_rounds=1, e2e_rounds=3, ci_rounds=2,
        ticket_cursor=5,
    )
    result = reset(task, ResetCause.PR_CYCLE_STARTED)

    assert result.ci_rounds == 0
    assert result.review_rounds == 2   # retained
    assert result.gate_rounds == 1     # retained
    assert result.e2e_rounds == 3      # retained
    assert result.ticket_cursor == 5   # metadata preserved
