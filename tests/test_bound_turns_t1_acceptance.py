"""Locked T1 acceptance: only the current bound main lifecycle supplies a stop.

These tests use public snapshots and events, never snapshot files or private
runtime implementation. Stopped-turn retirement belongs to T4.
"""

import json

import pytest

from dispatcher import main, waitd
from dispatcher.runtime_control import RuntimeControl
from dispatcher.state import has_waiting, load, mark_waiting
from tests.test_main import (FakeSessions, Stage, cfg, deps, make_task,
                             patch_usage, patch_workspace)


TARGET = "portfolio_eval"
ISSUE = 370
ROOT = "main-conversation-370"


def prepared(control, *, target=TARGET, issue=ISSUE, stage="review", ticket="",
             conversation_id=ROOT, worktree="/task/worktree"):
    snapshot = control.prepare(target, issue, stage, ticket=ticket,
                               runtime="codex", conversation_id=conversation_id,
                               worktree=worktree)
    binding = snapshot["binding"]
    assert control.event(binding, {"type": "service", "status": "live"})
    assert control.event(binding, {"type": "inventory", "certainty": "known",
                                   "workers": []})
    return binding


def started(control, binding, turn="turn-A"):
    assert control.event(binding, {"type": "turn/started",
                                   "thread_id": binding["conversation_id"],
                                   "turn_id": turn})


def completed(control, binding, turn="turn-A"):
    assert control.event(binding, {"type": "turn/completed",
                                   "thread_id": binding["conversation_id"],
                                   "turn_id": turn, "status": "completed"})


def view(control, binding):
    return control.view(binding["target"], binding["issue"], binding["launch_id"])


def test_t1_prepared_launch_binds_once_and_requires_authoritative_lifecycle(tmp_path):
    control = RuntimeControl(tmp_path)
    snapshot = control.prepare(TARGET, ISSUE, "implement", ticket="3",
                               worktree="/task/worktree")
    binding = snapshot["binding"]
    assert binding["target"] == TARGET
    assert binding["issue"] == ISSUE
    assert binding["stage"] == "implement"
    assert binding["ticket"] == "3"
    assert binding["runtime"] == "codex"
    assert binding["worktree"] == "/task/worktree"
    assert binding["launch_id"]
    assert binding["conversation_id"] is None
    assert snapshot["main"]["status"] == "unknown"
    assert control.event(binding, {"type": "bound", "conversation_id": ROOT})
    bound = control.view(TARGET, ISSUE)
    assert bound["binding"]["conversation_id"] == ROOT
    assert bound["main"]["status"] == "unknown"
    assert not control.event(bound["binding"], {"type": "bound",
                                               "conversation_id": "foreign-root"})
    assert control.view(TARGET, ISSUE) == bound


@pytest.mark.parametrize("event_type", ["turn/started", "turn/completed"])
def test_t1_old_turn_events_cannot_change_the_new_current_turn(tmp_path, event_type):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    started(control, binding)
    completed(control, binding)
    started(control, binding, "turn-B")
    before = view(control, binding)
    delayed = {"type": event_type, "thread_id": ROOT, "turn_id": "turn-A",
               "status": "completed"}
    assert not control.event(binding, delayed)
    assert view(control, binding) == before
    assert before["main"]["turn_id"] == "turn-B"
    assert before["main"]["status"] == "active"


@pytest.mark.parametrize("foreign_thread", ["title-thread", "child-thread",
                                            "independent-conversation"])
def test_t1_foreign_conversation_or_child_cannot_supply_main_stop(tmp_path,
                                                                foreign_thread):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    started(control, binding)
    before = view(control, binding)
    assert not control.event(binding, {"type": "turn/completed",
                                       "thread_id": foreign_thread,
                                       "turn_id": "turn-A", "status": "completed"})
    assert view(control, binding) == before


@pytest.mark.parametrize("notification", [
    {},
    {"type": "agent-turn-complete"},
    {"type": "agent-turn-complete", "thread-id": "title-thread"},
    {"type": "agent-turn-complete", "thread-id": ROOT},
])
def test_t1_unqualified_codex_notify_cannot_mutate_a_managed_launch(tmp_path,
                                                                  notification):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    started(control, binding)
    before = view(control, binding)
    waitd.handle_ping(json.dumps({"target": TARGET, "issue": ISSUE,
                                 "runtime": "codex", "mode": "waiting",
                                 "notification": notification}).encode(), tmp_path)
    assert view(control, binding) == before
    assert not has_waiting(tmp_path, TARGET, ISSUE)


@pytest.mark.parametrize("same_conversation", [False, True])
def test_t1_previous_ticket_or_launch_callbacks_cannot_mutate_replacement(tmp_path,
                                                                       same_conversation):
    control = RuntimeControl(tmp_path)
    old_binding = prepared(control, stage="implement", ticket="3")
    started(control, old_binding)
    root = ROOT if same_conversation else "ticket-4-conversation"
    new_binding = prepared(control, stage="implement", ticket="4",
                           conversation_id=root)
    started(control, new_binding, "turn-A")
    before = view(control, new_binding)
    assert new_binding["launch_id"] != old_binding["launch_id"]
    assert not control.event(old_binding, {"type": "turn/completed",
                                           "thread_id": ROOT, "turn_id": "turn-A",
                                           "status": "completed"})
    assert view(control, new_binding) == before


@pytest.mark.parametrize("other_target,other_issue", [
    (TARGET, 384), ("another_target", ISSUE),
])
def test_t1_concurrent_task_callback_cannot_end_another_tasks_turn(tmp_path,
                                                                 other_target,
                                                                 other_issue):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    other_binding = prepared(control, target=other_target, issue=other_issue,
                             conversation_id="other-main-conversation")
    started(control, binding)
    started(control, other_binding)
    before = view(control, binding)
    other_before = view(control, other_binding)
    assert not control.event(binding, {"type": "turn/completed",
                                       "thread_id": other_binding["conversation_id"],
                                       "turn_id": "turn-A", "status": "completed"})
    assert view(control, binding) == before
    assert view(control, other_binding) == other_before
    completed(control, other_binding)
    assert view(control, binding) == before
    assert view(control, other_binding)["main"]["status"] == "stopped"


def test_t1_new_bound_main_input_invalidates_previous_stopped_evidence(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    started(control, binding)
    completed(control, binding)
    assert view(control, binding)["main"]["status"] == "stopped"
    started(control, binding, "operator-turn-B")
    current = view(control, binding)
    assert current["main"]["turn_id"] == "operator-turn-B"
    assert current["main"]["status"] == "active"


def test_t1_only_normal_exact_current_completion_establishes_stopped_evidence(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    before = view(control, binding)
    assert not control.event(binding, {"type": "turn/completed", "thread_id": ROOT,
                                       "turn_id": "unstarted-turn", "status": "completed"})
    assert view(control, binding) == before
    started(control, binding)
    completed(control, binding)
    current = view(control, binding)
    assert current["main"]["turn_id"] == "turn-A"
    assert current["main"]["status"] == "stopped"


@pytest.mark.parametrize("bad_event", [
    {"type": "turn/completed", "thread_id": ROOT, "turn_id": "turn-A", "status": "failed"},
    {"type": "turn/completed", "thread_id": ROOT, "turn_id": "turn-A", "status": "interrupted"},
    {"type": "turn/completed", "thread_id": ROOT, "turn_id": "turn-A"},
    {"type": "turn/completed", "thread_id": ROOT, "status": "completed"},
    {"type": "turn/completed", "turn_id": "turn-A", "status": "completed"},
    {"type": "turn/completed", "thread_id": ROOT, "turn_id": None, "status": "completed"},
    {"type": "turn/completed", "thread_id": ROOT, "turn_id": {}, "status": "completed"},
    {"type": "notify", "thread_id": ROOT, "turn_id": "turn-A", "status": "completed"},
    {},
])
def test_t1_failed_or_malformed_event_cannot_establish_stopped_evidence(tmp_path,
                                                                     bad_event):
    control = RuntimeControl(tmp_path)
    binding = prepared(control)
    started(control, binding)
    before = view(control, binding)
    assert not control.event(binding, bad_event)
    assert view(control, binding) == before


@pytest.mark.parametrize("main_status", ["active", "unknown"])
def test_t1_dispatcher_ignores_legacy_waiting_marker_for_managed_unstopped_turn(
        tmp_path, monkeypatch, main_status):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    config = cfg(tmp_path)
    worktree = make_task(config, issue=ISSUE, stage=Stage.REVIEW)
    (worktree / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "working"}))
    control = RuntimeControl(config.state_dir)
    binding = prepared(control, worktree=str(worktree))
    if main_status == "active":
        started(control, binding)
    mark_waiting(config.state_dir, TARGET, ISSUE)

    class ManagedSessions(FakeSessions):
        def runtime_view(self, target, issue):
            return control.view(target, issue)

        def retire_runtime(self, binding, revision, *, reason="stopped", cap=10800):
            pytest.fail("An active or unknown main turn cannot request stopped-turn retirement")

    sessions = ManagedSessions(alive={ISSUE})
    main.run_pass(config, deps(sess=sessions))
    task = load(config.state_dir, TARGET, ISSUE)
    assert task.stage is Stage.REVIEW
    assert not task.park
    assert task.slot == 0
    assert sessions.ended == []
    assert sessions.resumed == []
    assert sessions.spawned == []
    assert control.view(TARGET, ISSUE)["main"]["status"] == main_status
