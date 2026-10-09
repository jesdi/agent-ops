"""Runtime lifecycle behavior through its public snapshot and event boundary."""

import json
from dataclasses import replace

import pytest

from dispatcher import main
from dispatcher.runtime_control import RuntimeControl
from dispatcher.sessions import Sessions
from dispatcher.state import has_waiting, load, read_background, read_session
from dispatcher.waitd import handle_ping
from tests.test_main import (FakeSessions, Stage, cfg, deps, make_task,
                             patch_usage, patch_workspace)


def test_prepared_service_and_known_empty_inventory_survive_listener_restart(tmp_path):
    control = RuntimeControl(tmp_path)
    snapshot = control.prepare("project", 1, "review", conversation_id="root")
    binding = snapshot["binding"]
    assert snapshot["service"] == "unknown"
    assert snapshot["inventory"] == "unknown"
    assert control.event(binding, {"type": "service", "status": "live"})
    assert control.event(binding, {"type": "inventory", "certainty": "known",
                                   "workers": []})
    restarted = RuntimeControl(tmp_path).view("project", 1, binding["launch_id"])
    assert restarted["service"] == "live"
    assert restarted["inventory"] == "known"
    assert restarted["workers"] == []


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_legacy_ping_cannot_record_session_or_background_for_prepared_launch(tmp_path, runtime):
    control = RuntimeControl(tmp_path)
    before = control.prepare("project", 1, "review", runtime=runtime)
    handle_ping(json.dumps({"target": "project", "issue": 1, "runtime": runtime,
                            "session_id": "foreign", "background_tasks": ["worker"]})
                .encode(), tmp_path)
    assert control.view("project", 1) == before
    assert not has_waiting(tmp_path, "project", 1)
    assert read_background(tmp_path, "project", 1) is None
    assert read_session(tmp_path, "project", 1) is None


def test_managed_unknown_turn_is_not_parked_by_legacy_stall_timer(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    config = replace(cfg(tmp_path), stall_after_seconds=600)
    worktree = make_task(config, issue=1, stage=Stage.REVIEW)
    (worktree / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "review", "status": "working"}))
    control = RuntimeControl(config.state_dir)
    control.prepare("portfolio_eval", 1, "review", worktree=str(worktree))

    class ManagedSessions(FakeSessions):
        def runtime_view(self, target, issue):
            return control.view(target, issue)

    sessions = ManagedSessions(alive={1}, idle={1: 601})
    main.run_pass(config, deps(sess=sessions))
    task = load(config.state_dir, "portfolio_eval", 1)
    assert task.stage is Stage.REVIEW
    assert not task.park
    assert sessions.ended == []


def test_authoritative_new_turn_supersedes_active_turn_after_non_normal_end(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root",
                                   "turn_id": "interrupted-A"})
    assert not control.event(binding, {"type": "turn/completed", "thread_id": "root",
                                       "turn_id": "interrupted-A", "status": "interrupted"})
    assert control.event(binding, {"type": "turn/started", "thread_id": "root",
                                   "turn_id": "operator-B"})
    before = control.view("project", 1)
    assert before["main"]["turn_id"] == "operator-B"
    assert before["main"]["status"] == "active"
    assert not control.event(binding, {"type": "turn/started", "thread_id": "root",
                                       "turn_id": "interrupted-A"})
    assert control.view("project", 1) == before


@pytest.mark.parametrize("status,alive,expected_stage,park", [
    ("blocked", True, Stage.REVIEW, "parked"),
    ("awaiting-ci", True, Stage.REVIEW, "awaiting-ci"),
    ("working", False, Stage.FAILED, ""),
])
def test_managed_launch_preserves_stage_and_crash_rules(
        tmp_path, monkeypatch, status, alive, expected_stage, park):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    config = cfg(tmp_path)
    worktree = make_task(config, issue=1, stage=Stage.REVIEW)
    (worktree / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": status, "run_id": 123, "note": "gate failed"}))
    control = RuntimeControl(config.state_dir)
    control.prepare("portfolio_eval", 1, "review", worktree=str(worktree))

    class ManagedSessions(FakeSessions):
        def runtime_view(self, target, issue):
            return control.view(target, issue)

    sessions = ManagedSessions(alive={1} if alive else set())
    main.run_pass(config, deps(sess=sessions))
    task = load(config.state_dir, "portfolio_eval", 1)
    assert task.stage is expected_stage
    assert task.park == park
    assert sessions.ended == [1]


def test_sessions_reads_only_its_task_launch_snapshot(tmp_path):
    sessions = Sessions(state_dir=tmp_path)
    assert sessions.runtime_view("project", 1) is None
    assert Sessions().runtime_view("project", 1) is None
    expected = RuntimeControl(tmp_path).prepare("project", 1, "review")
    assert sessions.runtime_view("project", 1) == expected
    assert sessions.runtime_view("other", 1) is None
    assert sessions.runtime_view("project", 2) is None


@pytest.mark.parametrize("field,value", [
    ("target", "foreign"), ("issue", 2), ("stage", "implement"),
    ("ticket", "4"), ("runtime", "claude"), ("worktree", "/foreign"),
    ("conversation_id", "foreign"), ("launch_id", "foreign"),
])
def test_every_binding_identity_field_fences_lifecycle_events(tmp_path, field, value):
    control = RuntimeControl(tmp_path)
    before = control.prepare("project", 1, "review", conversation_id="root")
    foreign = {**before["binding"], field: value}
    assert not control.event(foreign, {"type": "turn/started", "thread_id": "root",
                                       "turn_id": "A"})
    assert control.view("project", 1) == before


def test_duplicate_started_cannot_reactivate_stopped_turn_after_restart(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    assert control.event(binding, {"type": "turn/completed", "thread_id": "root",
                                   "turn_id": "A", "status": "completed"})
    restarted = RuntimeControl(tmp_path)
    before = restarted.view("project", 1)
    assert not restarted.event(binding, {"type": "turn/started", "thread_id": "root",
                                         "turn_id": "A"})
    assert restarted.view("project", 1) == before
