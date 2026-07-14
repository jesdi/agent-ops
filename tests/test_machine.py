from pathlib import Path

import pytest

from dispatcher.machine import (
    HandleCrash,
    NoOp,
    Notify,
    SetTaskStage,
    SpawnStage,
    next_actions,
)
from dispatcher.state import Stage, StageSignal, TaskState

GOOD_SPEC = "# t — design\n\n## Problem\n\n" + ("x " * 400) + "\n\n## Decisions\n\n" + ("y " * 400)
GOOD_PLAN = "# t Plan\n\n**Goal:** g\n\n### Task 1: a\n\n" + ("z " * 900)


def task(stage, worktree="/tmp/wt", issue=101):
    return TaskState(issue=issue, target="portfolio_eval", stage=stage, slot=0,
                     worktree=worktree, branch=f"agent/task-{issue}",
                     title="Add widget", updated_at="2026-07-14T12:00:00+00:00")


def sig(stage, status, artifact=""):
    return StageSignal(stage=stage, status=status, artifact=artifact)


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
    assert any(isinstance(a, Notify) and a.template == "awaiting_spec_review" for a in acts)
    # second pass, stage already updated → no re-notify
    again = next_actions(task(Stage.AWAITING_SPEC_REVIEW),
                         sig("spec", "awaiting-review"), True)
    assert again == [NoOp()]


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


def test_implement_done_is_pr_open():
    acts = next_actions(task(Stage.IMPLEMENT), sig("implement", "done"), True)
    assert SetTaskStage(Stage.PR_OPEN) in acts
    assert Notify("pr_opened") in acts


def test_blocked_escalates_in_place_once():
    acts = next_actions(task(Stage.IMPLEMENT),
                        StageSignal("implement", "blocked", note="need creds"), True)
    assert SetTaskStage(Stage.BLOCKED) in acts
    assert any(isinstance(a, Notify) and a.template == "stage_blocked" for a in acts)
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
