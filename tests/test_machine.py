from pathlib import Path

import pytest

from dataclasses import replace

from dispatcher.loops import Outcome
from dispatcher.machine import (
    ApplyDecision,
    HandleCrash,
    NoOp,
    Notify,
    ParkForCI,
    ParkForInput,
    ParkForReview,
    PublishSpec,
    RetryStage,
    SetTaskStage,
    SetTickets,
    SpawnStage,
    next_actions,
)
from dispatcher.state import LoopCaps, Stage, StageSignal, TaskState

GOOD_SPEC = "# t — design\n\n## Problem\n\n" + ("x " * 400) + "\n\n## Decisions\n\n" + ("y " * 400)
GOOD_TICKET = ("# 01 — thing\n\n**What to build:** " + ("behaviour " * 30)
               + "\n\n**Blocked by:** None\n\n- [ ] It works end to end\n")


def tickets(tmp_path, n=2, bad=False):
    d = tmp_path / ".agent" / "tickets"; d.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        (d / f"{i:02d}-t{i}.md").write_text("# tiny\n" if bad else GOOD_TICKET)
    return d


def task(stage, worktree="/tmp/wt", issue=101, park=""):
    return TaskState(issue=issue, target="portfolio_eval", stage=stage, slot=0,
                     worktree=worktree, branch=f"agent/task-{issue}",
                     title="Add widget", updated_at="2026-07-14T12:00:00+00:00",
                     park=park)


def sig(stage, status, artifact="", run_id=0):
    return StageSignal(stage=stage, status=status, artifact=artifact, run_id=run_id)


def test_dead_session_is_crash():
    acts = next_actions(task(Stage.SPEC), None, session_alive=False)
    assert acts == [HandleCrash()]


def test_dead_session_at_gate_respawns_spec():
    # VPS reboot recovery: gate-parked tasks are re-spawned into a fresh
    # spec session (draft spec is on disk), not treated as crashes.
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), session_alive=False)
    assert acts == [SpawnStage(Stage.SPEC)]


def test_dead_session_with_done_signal_is_not_crash(tmp_path):
    spec = tmp_path / "spec.md"; spec.write_text(GOOD_SPEC)
    acts = next_actions(task(Stage.SPEC, worktree=str(tmp_path)),
                        sig("spec", "done", str(spec)), session_alive=False)
    assert SpawnStage(Stage.PLAN) in acts


def test_working_is_noop():
    assert next_actions(task(Stage.PLAN), sig("plan", "working"), True) == [NoOp()]


def test_no_signal_yet_alive_is_noop():
    assert next_actions(task(Stage.SPEC), None, True) == [NoOp()]


def test_awaiting_review_notifies_once():
    acts = next_actions(task(Stage.SPEC), sig("spec", "awaiting-review"), True)
    assert SetTaskStage(Stage.AWAITING_SPEC_REVIEW) in acts
    assert PublishSpec() in acts
    assert any(isinstance(a, Notify) and a.template == "awaiting_spec_review" for a in acts)
    # second pass, stage already updated → no re-notify
    again = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                         sig("spec", "awaiting-review"), True)
    assert again == [NoOp()]


def test_spec_awaiting_review_publishes_between_stage_and_notify():
    acts = next_actions(task(Stage.SPEC),
                        sig("spec", "awaiting-review",
                            artifact="docs/superpowers/specs/x-design.md"),
                        session_alive=True)
    assert acts == [
        SetTaskStage(Stage.AWAITING_SPEC_REVIEW,
                     artifact="docs/superpowers/specs/x-design.md"),
        PublishSpec(artifact="docs/superpowers/specs/x-design.md"),
        Notify("awaiting_spec_review", ""),
    ]


def test_gate_and_non_spec_stages_do_not_publish():
    # already at the gate: no re-publish on every pass
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), session_alive=True)
    assert PublishSpec() not in acts and acts == [NoOp()]
    # misrouted awaiting-review from a non-SPEC stage: still ignored
    acts = next_actions(task(Stage.PLAN),
                        sig("plan", "awaiting-review"), session_alive=True)
    assert acts == [NoOp()]


def test_spec_done_valid_spawns_plan(tmp_path):
    spec = tmp_path / "spec.md"; spec.write_text(GOOD_SPEC)
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW, worktree=str(tmp_path)),
                        sig("spec", "done", str(spec)), True)
    assert acts == [SpawnStage(Stage.PLAN)]


def test_spec_done_invalid_fails(tmp_path):
    spec = tmp_path / "spec.md"; spec.write_text("tiny")
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW, worktree=str(tmp_path)),
                        sig("spec", "done", str(spec)), True)
    assert SetTaskStage(Stage.FAILED) in acts
    assert any(isinstance(a, Notify) and a.template == "artifact_failed" for a in acts)


def test_plan_done_valid_tickets_starts_ticket_one(tmp_path):
    tickets(tmp_path, n=3)
    acts = next_actions(task(Stage.PLAN, worktree=str(tmp_path)),
                        sig("plan", "done", ".agent/tickets"), True)
    assert acts == [SetTickets(1, 3), SpawnStage(Stage.IMPLEMENT, ticket=1),
                    Notify("implement_started", "3 ticket(s)")]


def test_plan_done_malformed_tickets_retries_then_fails(tmp_path):
    tickets(tmp_path, n=2, bad=True)
    acts = next_actions(task(Stage.PLAN, worktree=str(tmp_path)),
                        sig("plan", "done", ".agent/tickets"), True)
    assert len(acts) == 1 and isinstance(acts[0], RetryStage) and acts[0].reason
    t = replace(task(Stage.PLAN, worktree=str(tmp_path)), plan_retries=1)
    acts = next_actions(t, sig("plan", "done", ".agent/tickets"), True)
    assert SetTaskStage(Stage.FAILED) in acts
    assert any(isinstance(a, Notify) and a.template == "artifact_failed" for a in acts)


def test_plan_done_with_a_numbering_gap_is_malformed(tmp_path):
    d = tickets(tmp_path, n=1)
    (d / "03-late.md").write_text(GOOD_TICKET)
    acts = next_actions(task(Stage.PLAN, worktree=str(tmp_path)),
                        sig("plan", "done", ".agent/tickets"), True)
    assert isinstance(acts[0], RetryStage) and "contiguous" in acts[0].reason


def test_implement_done_advances_the_ticket_cursor():
    t = replace(task(Stage.IMPLEMENT), ticket_cursor=1, ticket_count=3)
    acts = next_actions(t, sig("implement", "done"), True)
    assert acts == [SetTickets(2, 3), SpawnStage(Stage.IMPLEMENT, ticket=2)]


def test_last_ticket_done_spawns_review():
    t = replace(task(Stage.IMPLEMENT), ticket_cursor=3, ticket_count=3)
    acts = next_actions(t, StageSignal("implement", "done", note="all green"), True)
    assert acts == [SpawnStage(Stage.REVIEW), Notify("review_started", "all green")]


def test_blocked_ticket_parks_without_touching_the_cursor():
    t = replace(task(Stage.IMPLEMENT), ticket_cursor=2, ticket_count=5)
    acts = next_actions(t, StageSignal("implement", "blocked", note="secret missing"), True)
    assert acts == [ParkForInput("secret missing")]
    assert not any(isinstance(a, (SetTickets, SetTaskStage)) for a in acts)


def test_review_done_is_pr_open():
    acts = next_actions(task(Stage.REVIEW),
                        StageSignal("review", "done", note="https://github.com/o/r/pull/9",
                                    artifact="https://github.com/o/r/pull/9"), True)
    assert acts == [SetTaskStage(Stage.PR_OPEN), Notify("pr_opened", "https://github.com/o/r/pull/9")]


def test_review_dead_session_is_crash():
    assert next_actions(task(Stage.REVIEW), None, False) == [HandleCrash()]


def test_review_awaiting_ci_parks():
    assert next_actions(task(Stage.REVIEW), sig("review", "awaiting-ci", run_id=5), True) == [ParkForCI(5)]


def test_awaiting_answers_parks_with_the_artifact():
    acts = next_actions(task(Stage.SPEC),
                        StageSignal("spec", "awaiting-answers", note="7 questions",
                                    artifact=".agent/questionnaire.md"), True)
    assert acts == [ParkForInput("7 questions", artifact=".agent/questionnaire.md", is_answers=True)]


def loop_sig(loop, n, stage="implement"):
    return StageSignal(stage, "working", loop=loop, round=n)


def test_new_gate_round_is_recorded():
    acts = next_actions(task(Stage.IMPLEMENT), loop_sig("gate", 1), True)
    assert len(acts) == 1 and isinstance(acts[0], ApplyDecision)
    assert acts[0].decision.outcome is Outcome.WITHIN_LIMIT
    assert acts[0].decision.round == 1


def test_last_gate_round_records_and_pings():
    acts = next_actions(task(Stage.IMPLEMENT), loop_sig("gate", 2), True)
    assert len(acts) == 1 and isinstance(acts[0], ApplyDecision)
    assert acts[0].decision.outcome is Outcome.LAST_ROUND
    assert acts[0].decision.round == 2


def test_gate_round_past_the_cap_parks():
    t = replace(task(Stage.IMPLEMENT), gate_rounds=2)
    acts = next_actions(t, loop_sig("gate", 3), True)
    assert len(acts) == 1 and isinstance(acts[0], ApplyDecision)
    assert acts[0].decision.outcome is Outcome.EXHAUSTED
    assert acts[0].decision.round == 3


def test_counters_survive_a_resume():
    # The session re-reports a round the dispatcher already counted: nothing
    # is re-recorded and nothing resets — the budget is the task state's.
    t = replace(task(Stage.IMPLEMENT), gate_rounds=2)
    assert next_actions(t, loop_sig("gate", 2), True) == [NoOp()]
    assert next_actions(t, loop_sig("gate", 1), True) == [NoOp()]


def test_review_loop_uses_its_own_cap():
    t = replace(task(Stage.REVIEW), review_rounds=1)
    acts = next_actions(t, loop_sig("review", 2, stage="review"), True,
                        caps=LoopCaps(review=1))
    assert len(acts) == 1 and isinstance(acts[0], ApplyDecision)
    assert acts[0].decision.outcome is Outcome.EXHAUSTED
    assert acts[0].decision.round == 2


def test_unknown_loop_is_ignored():
    assert next_actions(task(Stage.IMPLEMENT), loop_sig("dance", 9), True) == [NoOp()]


def test_round_report_then_waiting_records_and_parks_for_input():
    acts = next_actions(task(Stage.IMPLEMENT), loop_sig("gate", 1), True, waiting=True)
    assert len(acts) == 2 and isinstance(acts[0], ApplyDecision)
    assert acts[0].decision.outcome is Outcome.WITHIN_LIMIT
    assert acts[1] == ParkForInput("(session stopped mid-stage waiting for input)")


def test_round_on_a_done_signal_is_not_a_round():
    t = replace(task(Stage.IMPLEMENT), ticket_cursor=1, ticket_count=1)
    acts = next_actions(t, StageSignal("implement", "done", loop="gate", round=9), True)
    assert acts == [SpawnStage(Stage.REVIEW), Notify("review_started", "")]


def test_blocked_parks_and_legacy_stage_is_noop():
    # blocked signal now emits ParkForInput (park lifecycle replaces escalate-in-place)
    acts = next_actions(task(Stage.IMPLEMENT),
                        StageSignal("implement", "blocked", note="need creds"), True)
    assert acts == [ParkForInput("need creds")]
    # a task already in Stage.BLOCKED (legacy) is still a no-op
    again = next_actions(task(Stage.BLOCKED),
                         StageSignal("implement", "blocked", note="need creds"), True)
    assert again == [NoOp()]


def test_relative_artifact_resolved_against_worktree(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(GOOD_SPEC)
    acts = next_actions(task(Stage.SPEC, worktree=str(tmp_path)),
                        sig("spec", "done", "docs/spec.md"), True)
    assert acts == [SpawnStage(Stage.PLAN)]


def test_done_signal_on_blocked_stage_is_noop():
    # BLOCKED is in IN_FLIGHT_STAGES but not in NEXT_STAGE; must not KeyError.
    acts = next_actions(task(Stage.BLOCKED), sig("implement", "done"), True)
    assert acts == [NoOp()]


def test_awaiting_review_on_non_spec_stage_is_noop():
    # Only SPEC should transition to AWAITING_SPEC_REVIEW; stale/misrouted signals are ignored.
    acts = next_actions(task(Stage.PLAN), sig("plan", "awaiting-review"), True)
    assert acts == [NoOp()]


def test_blocked_parks():
    acts = next_actions(task(Stage.IMPLEMENT), sig("implement", "blocked"), True)
    assert acts == [ParkForInput("")]


def test_legacy_blocked_stage_is_noop():
    assert next_actions(task(Stage.BLOCKED), sig("implement", "blocked"), True) == [NoOp()]


def test_awaiting_ci_parks_with_run_id():
    acts = next_actions(task(Stage.IMPLEMENT),
                        sig("implement", "awaiting-ci", run_id=4242), True)
    assert acts == [ParkForCI(4242)]


def test_awaiting_ci_without_run_id_fails_stage():
    acts = next_actions(task(Stage.IMPLEMENT), sig("implement", "awaiting-ci"), True)
    assert SetTaskStage(Stage.FAILED) in acts


def test_waiting_marker_parks_working_session():
    acts = next_actions(task(Stage.SPEC), sig("spec", "working"), True, waiting=True)
    assert acts == [ParkForInput("(session stopped mid-stage waiting for input)")]


def test_waiting_marker_ignored_when_stage_done(tmp_path):
    spec = tmp_path / "s.md"; spec.write_text(GOOD_SPEC)
    acts = next_actions(task(Stage.SPEC, worktree=str(tmp_path)),
                        sig("spec", "done", str(spec)), True, waiting=True)
    assert SpawnStage(Stage.PLAN) in acts


def test_parked_task_is_noop_even_with_dead_session():
    from dataclasses import replace
    from dispatcher.state import PARK_CI
    t = replace(task(Stage.IMPLEMENT), park=PARK_CI)
    assert next_actions(t, sig("implement", "awaiting-ci", run_id=1), False) == [NoOp()]


def test_awaiting_review_carries_spec_artifact():
    acts = next_actions(
        task(Stage.SPEC),
        sig("spec", "awaiting-review",
            artifact="/tmp/wt/docs/superpowers/specs/x-design.md"), True)
    gate = [a for a in acts if isinstance(a, SetTaskStage)][0]
    assert gate.stage is Stage.AWAITING_SPEC_REVIEW
    assert gate.artifact == "/tmp/wt/docs/superpowers/specs/x-design.md"


def test_stall_no_signal_parks():
    acts = next_actions(task(Stage.SPEC), None, True, idle_seconds=601.0)
    assert len(acts) == 1 and isinstance(acts[0], ParkForInput)
    assert "no session output for 10m" in acts[0].note


def test_stall_working_without_waiting_parks():
    acts = next_actions(task(Stage.PLAN), sig("plan", "working"), True,
                        idle_seconds=601.0)
    assert len(acts) == 1 and isinstance(acts[0], ParkForInput)


def test_stall_under_threshold_is_noop():
    assert next_actions(task(Stage.SPEC), None, True,
                        idle_seconds=599.0) == [NoOp()]
    assert next_actions(task(Stage.PLAN), sig("plan", "working"), True,
                        idle_seconds=599.0) == [NoOp()]


def test_stall_at_exact_threshold_is_noop():
    # strictly greater-than: the boundary second itself does not park
    assert next_actions(task(Stage.SPEC), None, True,
                        idle_seconds=600.0) == [NoOp()]
    assert next_actions(task(Stage.SPEC), None, True,
                        idle_seconds=30.0, stall_after=30.0) == [NoOp()]


def test_stall_none_idle_never_parks():
    # query failure / dry-run — unknown is not stalled
    assert next_actions(task(Stage.SPEC), None, True,
                        idle_seconds=None) == [NoOp()]


def test_stall_zero_threshold_disables():
    assert next_actions(task(Stage.SPEC), None, True,
                        idle_seconds=1e9, stall_after=0) == [NoOp()]


def test_stall_ignores_gated_statuses():
    # idle-by-design states never stall-park
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), True, idle_seconds=1e9)
    assert acts == [NoOp()]
    acts = next_actions(task(Stage.IMPLEMENT),
                        sig("implement", "awaiting-ci", run_id=7), True,
                        idle_seconds=1e9)
    assert acts == [ParkForCI(7)]  # CI rule, not the stall rule


def test_stall_working_with_waiting_uses_waiting_park():
    acts = next_actions(task(Stage.SPEC), sig("spec", "working"), True,
                        waiting=True, idle_seconds=1e9)
    assert acts == [ParkForInput("(session stopped mid-stage waiting for input)")]


def test_stall_parked_task_still_noop():
    t = task(Stage.IMPLEMENT, park="parked")
    assert next_actions(t, None, True, idle_seconds=1e9) == [NoOp()]


def test_stall_dead_session_is_still_crash():
    acts = next_actions(task(Stage.SPEC), None, False, idle_seconds=1e9)
    assert acts == [HandleCrash()]


def test_gate_parks_once_the_grace_period_elapses():
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), session_alive=True,
                        grace_elapsed=True)
    assert acts == [ParkForReview()]


def test_gate_waits_inside_the_grace_period():
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), session_alive=True,
                        grace_elapsed=False)
    assert acts == [NoOp()]


def test_gate_parked_task_is_never_re_parked():
    # park short-circuit wins over the grace rule: wake/resume is
    # dispatcher-side, and re-parking would re-send the ping every pass.
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW, park="awaiting-review"),
                        sig("spec", "awaiting-review"), session_alive=True,
                        grace_elapsed=True)
    assert acts == [NoOp()]


def test_dead_session_at_gate_still_respawns_even_after_grace():
    # Crash-during-grace: reboot recovery wins; a dead session has nothing
    # to park.
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                        sig("spec", "awaiting-review"), session_alive=False,
                        grace_elapsed=True)
    assert acts == [SpawnStage(Stage.SPEC)]


def test_approved_spec_advances_instead_of_parking(tmp_path):
    # The operator approved in-session just as the grace expired: a `done`
    # signal must still advance to PLAN, never park.
    spec = tmp_path / "spec.md"; spec.write_text(GOOD_SPEC)
    acts = next_actions(task(Stage.AWAITING_SPEC_REVIEW, worktree=str(tmp_path)),
                        sig("spec", "done", str(spec)), session_alive=True,
                        grace_elapsed=True)
    assert acts == [SpawnStage(Stage.PLAN)]


def test_grace_does_not_park_a_task_still_in_the_spec_stage():
    # Before the gate flip there is nothing to review — the first pass must
    # flip the stage and notify, not park.
    acts = next_actions(task(Stage.SPEC), sig("spec", "awaiting-review"),
                        session_alive=True, grace_elapsed=True)
    assert SetTaskStage(Stage.AWAITING_SPEC_REVIEW, artifact="") in acts
    assert not any(isinstance(a, ParkForReview) for a in acts)


def test_address_review_done_returns_to_pr_open():
    acts = next_actions(task(Stage.ADDRESS_REVIEW), StageSignal("address-review", "done", note="fixed nits"), True)
    assert acts == [SetTaskStage(Stage.PR_OPEN), Notify("pr_updated", "fixed nits")]


def test_address_review_awaiting_ci_parks():
    acts = next_actions(task(Stage.ADDRESS_REVIEW), sig("address-review", "awaiting-ci", run_id=99), True)
    assert acts == [ParkForCI(99)]


def test_address_review_blocked_parks_for_input():
    acts = next_actions(task(Stage.ADDRESS_REVIEW), StageSignal("address-review", "blocked", note="need key"), True)
    assert acts == [ParkForInput("need key")]


def test_address_review_dead_session_is_crash():
    acts = next_actions(task(Stage.ADDRESS_REVIEW), None, False)
    assert acts == [HandleCrash()]
