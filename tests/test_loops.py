"""Pure loop-policy tests: concrete TaskState in, expected Decision out. No
mocking, no inspecting the private field mapping — only the public interface
(SessionRound / evaluate / apply / Decision / Outcome)."""
from dataclasses import replace

from dispatcher.loops import (Decision, FailedRun, Outcome, PRAttention, SessionRound, apply, evaluate)
from dispatcher.state import LoopCaps, Stage, TaskState


def task(**kw):
    base = TaskState(issue=101, target="portfolio_eval", stage=Stage.IMPLEMENT,
                     slot=0, worktree="/tmp/wt", branch="agent/task-101",
                     title="Add widget", updated_at="2026-07-14T12:00:00+00:00")
    return replace(base, **kw)


def sr(loop, round, status="working"):
    return SessionRound(loop=loop, round=round, status=status)


# --- evaluate: known loops advance -----------------------------------------

def test_first_round_within_limit():
    d = evaluate(task(), sr("gate", 1), LoopCaps(gate=2))
    assert d == Decision("gate", Outcome.WITHIN_LIMIT, round=1, cap=2)


def test_last_round_is_flagged():
    d = evaluate(task(), sr("gate", 2), LoopCaps(gate=2))
    assert d == Decision("gate", Outcome.LAST_ROUND, round=2, cap=2)


def test_over_the_cap_is_exhausted():
    d = evaluate(task(gate_rounds=2), sr("gate", 3), LoopCaps(gate=2))
    assert d == Decision("gate", Outcome.EXHAUSTED, round=3, cap=2)


def test_each_loop_reads_its_own_counter_and_cap():
    d = evaluate(task(review_rounds=1), sr("review", 2), LoopCaps(review=1))
    assert d == Decision("review", Outcome.EXHAUSTED, round=2, cap=1)


def test_e2e_and_ci_loops_are_known():
    assert evaluate(task(), sr("e2e", 1), LoopCaps(e2e=3)).outcome is Outcome.WITHIN_LIMIT
    assert evaluate(task(), sr("ci", 3), LoopCaps(ci=3)).outcome is Outcome.LAST_ROUND


# --- evaluate: ignored inputs ----------------------------------------------

def test_unknown_loop_is_unchanged():
    assert evaluate(task(), sr("dance", 9), LoopCaps()).outcome is Outcome.UNCHANGED


def test_non_working_status_is_unchanged():
    # A loop+round on an awaiting-ci / done / blocked signal must not count.
    for status in ("awaiting-ci", "done", "blocked", "awaiting-review"):
        assert evaluate(task(), sr("gate", 5, status=status),
                        LoopCaps(gate=2)).outcome is Outcome.UNCHANGED


# --- evaluate: absolute accounting -----------------------------------------

def test_duplicate_report_is_unchanged():
    assert evaluate(task(gate_rounds=2), sr("gate", 2),
                    LoopCaps(gate=3)).outcome is Outcome.UNCHANGED


def test_lower_report_is_unchanged():
    # A resumed session re-reporting an old round can never rewind the budget.
    assert evaluate(task(gate_rounds=2), sr("gate", 1),
                    LoopCaps(gate=3)).outcome is Outcome.UNCHANGED


def test_jump_records_the_reported_value_not_incremented():
    # round=5 with have=2 records 5 (not have+1=3). cap=10 → still within limit.
    d = evaluate(task(gate_rounds=2), sr("gate", 5), LoopCaps(gate=10))
    assert d == Decision("gate", Outcome.WITHIN_LIMIT, round=5, cap=10)


def test_jump_past_the_cap_is_exhausted_at_reported_value():
    d = evaluate(task(gate_rounds=2), sr("gate", 5), LoopCaps(gate=4))
    assert d == Decision("gate", Outcome.EXHAUSTED, round=5, cap=4)


# --- evaluate: zero cap -----------------------------------------------------

def test_zero_cap_exhausts_on_the_first_round_without_last_round():
    d = evaluate(task(), sr("gate", 1), LoopCaps(gate=0))
    assert d == Decision("gate", Outcome.EXHAUSTED, round=1, cap=0)
    assert d.outcome is not Outcome.LAST_ROUND


# --- apply ------------------------------------------------------------------

def test_apply_writes_only_the_named_counter():
    t = task(gate_rounds=1, review_rounds=5, ci_rounds=2, check_cursor="abc")
    out = apply(t, Decision("gate", Outcome.WITHIN_LIMIT, round=2, cap=3))
    assert out.gate_rounds == 2
    # Every other field is preserved (a PR cursor a caller advanced survives).
    assert (out.review_rounds, out.ci_rounds, out.check_cursor) == (5, 2, "abc")


def test_apply_records_a_jump_value():
    out = apply(task(gate_rounds=2), Decision("gate", Outcome.EXHAUSTED, round=5, cap=4))
    assert out.gate_rounds == 5


def test_apply_is_a_noop_for_unchanged():
    t = task(gate_rounds=1)
    assert apply(t, Decision("", Outcome.UNCHANGED)) is t


# --- FailedRun: increment observation ---------------------------------------

def fr(loop, detail="run 7 failure"):
    return FailedRun(loop=loop, detail=detail)


def test_failed_run_e2e_increments_e2e_counter():
    d = evaluate(task(e2e_rounds=0), fr("e2e"), LoopCaps(e2e=3))
    assert d == Decision("e2e", Outcome.WITHIN_LIMIT, round=1, cap=3, detail="run 7 failure")


def test_failed_run_ci_increments_ci_counter():
    d = evaluate(task(ci_rounds=2), fr("ci"), LoopCaps(ci=5))
    assert d == Decision("ci", Outcome.WITHIN_LIMIT, round=3, cap=5, detail="run 7 failure")


def test_failed_run_last_round_at_cap():
    d = evaluate(task(e2e_rounds=1), fr("e2e"), LoopCaps(e2e=2))
    assert d == Decision("e2e", Outcome.LAST_ROUND, round=2, cap=2, detail="run 7 failure")


def test_failed_run_exhaustion_over_cap():
    d = evaluate(task(e2e_rounds=2), fr("e2e"), LoopCaps(e2e=2))
    assert d == Decision("e2e", Outcome.EXHAUSTED, round=3, cap=2, detail="run 7 failure")


def test_failed_run_zero_cap_exhausts_immediately_no_last_round():
    d = evaluate(task(e2e_rounds=0), fr("e2e"), LoopCaps(e2e=0))
    assert d.outcome is Outcome.EXHAUSTED
    assert d.outcome is not Outcome.LAST_ROUND


# --- PRAttention: increment on "ci" loop ------------------------------------

def pa(detail="check-failed on PR #42"):
    return PRAttention(detail=detail)


def test_pr_attention_increments_ci_counter():
    d = evaluate(task(ci_rounds=0), pa(), LoopCaps(ci=3))
    assert d == Decision("ci", Outcome.WITHIN_LIMIT, round=1, cap=3, detail="check-failed on PR #42")


def test_pr_attention_last_round_at_cap():
    d = evaluate(task(ci_rounds=1), pa(), LoopCaps(ci=2))
    assert d == Decision("ci", Outcome.LAST_ROUND, round=2, cap=2, detail="check-failed on PR #42")


def test_pr_attention_exhaustion_over_cap():
    d = evaluate(task(ci_rounds=2), pa(), LoopCaps(ci=2))
    assert d == Decision("ci", Outcome.EXHAUSTED, round=3, cap=2, detail="check-failed on PR #42")


def test_pr_attention_zero_cap_exhausts_immediately():
    d = evaluate(task(ci_rounds=0), pa(), LoopCaps(ci=0))
    assert d.outcome is Outcome.EXHAUSTED
    assert d.outcome is not Outcome.LAST_ROUND


def test_pr_attention_only_touches_ci_counter():
    # apply preserves all other counters (same guarantee as FailedRun)
    t = task(ci_rounds=0, review_rounds=3, gate_rounds=1, check_cursor="ts1")
    d = evaluate(t, pa(), LoopCaps(ci=5))
    out = apply(t, d)
    assert out.ci_rounds == 1
    assert (out.review_rounds, out.gate_rounds, out.check_cursor) == (3, 1, "ts1")
