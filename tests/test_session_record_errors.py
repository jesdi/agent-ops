"""Session persistence failures must not stop existing turn-marker updates."""
import json
import os

import pytest

from dispatcher.state import (SessionRecord, Stage, TaskState, has_waiting,
                              mark_background, mark_waiting, read_background,
                              read_session, save)
from dispatcher.waitd import handle_ping

TARGET = "portfolio_eval"
ISSUE = 370
FIRST_ID = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
SECOND_ID = "9d8c7b6a-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
BACKGROUND_TASK = {"id": "gate-1", "type": "shell", "command": "make crap-gate"}


def _task(state_dir):
    save(state_dir, TaskState(
        issue=ISSUE, target=TARGET, stage=Stage.IMPLEMENT, slot=0,
        worktree="/home/agent/repos/portfolio_eval.worktrees/task-370",
        branch="agent/task-370", title="Record the task's conversation",
        updated_at="2026-10-06T09:00:00+00:00",
    ))


def _ping(state_dir, session_id, background_tasks=None):
    handle_ping(json.dumps({
        "issue": ISSUE, "target": TARGET, "session_id": session_id,
        "background_tasks": background_tasks,
    }).encode(), state_dir)


def _prior_marker(state_dir, background_tasks):
    if background_tasks:
        mark_waiting(state_dir, TARGET, ISSUE)
    else:
        mark_background(state_dir, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)


def _assert_markers(state_dir, background_tasks):
    assert has_waiting(state_dir, TARGET, ISSUE) is (background_tasks is None)
    background = read_background(state_dir, TARGET, ISSUE)
    if background_tasks:
        assert background is not None
        assert background.tasks == ("gate-1",)
    else:
        assert background is None


def test_deeply_nested_rollout_keeps_prior_record_and_reports_waiting(tmp_path):
    _task(tmp_path)
    _ping(tmp_path, FIRST_ID)
    _prior_marker(tmp_path, None)
    rollouts = tmp_path / "codex-home" / "sessions" / "2026" / "10" / "06"
    rollouts.mkdir(parents=True)
    (rollouts / f"rollout-2026-10-06T09-00-00-{SECOND_ID}.jsonl").write_text(
        "[" * 10000 + "]" * 10000 + "\n")

    handle_ping(json.dumps({
        "issue": ISSUE, "target": TARGET, "session_id": SECOND_ID, "runtime": "codex",
    }).encode(), tmp_path)

    _assert_markers(tmp_path, None)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")


@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]],
                         ids=["waiting", "background"])
@pytest.mark.parametrize("broken_state", [
    '{"stage":', '{}', '[]', '{"stage": "unknown"}', '{"stage": "implement"}',
    "[" * 10000 + "]" * 10000,
], ids=["truncated-json", "missing-stage", "non-object", "unknown-stage", "missing-fields",
        "deeply-nested"])
def test_bad_task_state_keeps_prior_session_and_updates_turn_markers(
        tmp_path, capsys, background_tasks, broken_state):
    _task(tmp_path)
    _ping(tmp_path, FIRST_ID)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    _prior_marker(tmp_path, background_tasks)
    (tmp_path / "task-portfolio_eval-370.json").write_text(broken_state)

    _ping(tmp_path, SECOND_ID, background_tasks)

    _assert_markers(tmp_path, background_tasks)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    error = capsys.readouterr().err
    assert "waitd:" in error and TARGET in error and str(ISSUE) in error


@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]],
                         ids=["waiting", "background"])
def test_malformed_operator_request_keeps_session_and_updates_turn_markers(
        tmp_path, capsys, background_tasks):
    _task(tmp_path)
    _ping(tmp_path, FIRST_ID)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    _prior_marker(tmp_path, background_tasks)
    task_path = tmp_path / "task-portfolio_eval-370.json"
    task_doc = json.loads(task_path.read_text())
    task_doc["operator_request"] = []
    task_path.write_text(json.dumps(task_doc))

    _ping(tmp_path, SECOND_ID, background_tasks)

    _assert_markers(tmp_path, background_tasks)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    error = capsys.readouterr().err
    assert "waitd:" in error and TARGET in error and str(ISSUE) in error


@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]],
                         ids=["waiting", "background"])
def test_unreadable_task_state_still_updates_turn_markers(
        tmp_path, capsys, background_tasks):
    (tmp_path / "task-portfolio_eval-370.json").mkdir()
    _prior_marker(tmp_path, background_tasks)

    _ping(tmp_path, FIRST_ID, background_tasks)

    _assert_markers(tmp_path, background_tasks)
    assert read_session(tmp_path, TARGET, ISSUE) is None
    error = capsys.readouterr().err
    assert "waitd:" in error and TARGET in error and str(ISSUE) in error


@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]],
                         ids=["waiting", "background"])
def test_unwritable_session_record_still_updates_turn_markers(
        tmp_path, capsys, background_tasks):
    _task(tmp_path)
    (tmp_path / "session-portfolio_eval-370").mkdir()
    _prior_marker(tmp_path, background_tasks)

    _ping(tmp_path, FIRST_ID, background_tasks)

    _assert_markers(tmp_path, background_tasks)
    assert read_session(tmp_path, TARGET, ISSUE) is None
    error = capsys.readouterr().err
    assert "waitd:" in error and TARGET in error and str(ISSUE) in error


@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]],
                         ids=["waiting", "background"])
def test_failed_record_replacement_keeps_prior_session_and_updates_turn_markers(
        tmp_path, monkeypatch, capsys, background_tasks):
    _task(tmp_path)
    _ping(tmp_path, FIRST_ID)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    _prior_marker(tmp_path, background_tasks)
    replace_file = os.replace

    def failed_session_replace(src, dst):
        if str(dst).endswith("session-portfolio_eval-370"):
            raise OSError("session record storage unavailable")
        return replace_file(src, dst)

    monkeypatch.setattr(os, "replace", failed_session_replace)

    _ping(tmp_path, SECOND_ID, background_tasks)

    _assert_markers(tmp_path, background_tasks)
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(FIRST_ID, "implement")
    error = capsys.readouterr().err
    assert "waitd:" in error and TARGET in error and str(ISSUE) in error
