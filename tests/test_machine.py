from pathlib import Path

import pytest

from dataclasses import replace

from dispatcher.machine import (
    HandleCrash,
    NoOp,
    Notify,
    ParkForCI,
    ParkForInput,
    ParkForReview,
    PublishSpec,
    RetryStage,
    SetTaskStage,
    SpawnStage,
    next_actions,
)
from dispatcher.state import Stage, StageSignal, TaskState

GOOD_SPEC = "# t — design\n\n## Problem\n\n" + ("x " * 400) + "\n\n## Decisions\n\n" + ("y " * 400)
GOOD_PLAN = "# t Plan\n\n**Goal:** g\n\n### Task 1: a\n\n" + ("z " * 900)


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


def test_plan_done_valid_spawns_implement_and_notifies(tmp_path):
    plan = tmp_path / ".agent"; plan.mkdir()
    p = plan / "plan.md"; p.write_text(GOOD_PLAN)
    acts = next_actions(task(Stage.PLAN, worktree=str(tmp_path)),
                        sig("plan", "done", str(p)), True)
    assert SpawnStage(Stage.IMPLEMENT) in acts
    assert any(isinstance(a, Notify) and a.template == "implement_started" for a in acts)


def test_plan_done_bad_format_retries_in_session_first(tmp_path):
    # Well-sized plan but H2 task headings / no Goal → format check fails.
    # First failure resumes the session rather than failing the task.
    plan = tmp_path / ".agent"; plan.mkdir()
    p = plan / "plan.md"; p.write_text("# t\n\n## Task 1: a\n\n" + ("z " * 900))
    acts = next_actions(task(Stage.PLAN, worktree=str(tmp_path)),
                        sig("plan", "done", str(p)), True)
    assert len(acts) == 1 and isinstance(acts[0], RetryStage)
    assert acts[0].stage is Stage.PLAN and acts[0].reason


def test_plan_done_bad_format_fails_after_retry_exhausted(tmp_path):
    plan = tmp_path / ".agent"; plan.mkdir()
    p = plan / "plan.md"; p.write_text("# t\n\n## Task 1: a\n\n" + ("z " * 900))
    t = replace(task(Stage.PLAN, worktree=str(tmp_path)), plan_retries=1)
    acts = next_actions(t, sig("plan", "done", str(p)), True)
    assert SetTaskStage(Stage.FAILED) in acts
    assert any(isinstance(a, Notify) and a.template == "artifact_failed"
               for a in acts)


def test_implement_done_is_pr_open():
    acts = next_actions(task(Stage.IMPLEMENT), sig("implement", "done"), True)
    assert SetTaskStage(Stage.PR_OPEN) in acts
    assert Notify("pr_opened") in acts


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
