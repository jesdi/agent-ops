"""T05 review regressions through run_pass and its session launch boundary."""

from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import messages
from dispatcher.state import Stage, load, save
from tests.test_session_restart_acceptance import (
    KINDS, MODELS, RECORDS, RecordingSessions, _arrange, _assert_delivered, _pass,
)


class LaunchBoundarySessions(RecordingSessions):
    """Observe the public queue when a fresh session is about to start."""

    def __init__(self, state_dir, *, reject=False):
        super().__init__()
        self.state_dir = state_dir
        self.reject = reject
        self.at_launch = []

    def spawn_stage(self, target, issue, worktree, prompt, stage_name,
                    model, effort="", second=None):
        self.at_launch.append({
            "target": target, "issue": issue, "worktree": worktree,
            "prompt": prompt,
            "messages": messages.all_messages(self.state_dir, target, issue),
        })
        if self.reject:
            raise RuntimeError("Synthetic fresh session launch failure")
        super().spawn_stage(target, issue, worktree, prompt, stage_name,
                            model, effort, second)


def _observe_launch(c, dependencies, *, reject=False):
    sessions = LaunchBoundarySessions(c.state_dir, reject=reject)
    sessions.alive_set.update(dependencies.sessions.alive_set)
    dependencies.sessions = sessions
    return sessions


def _assert_pending_at_launch(sessions, before, queued):
    assert len(sessions.at_launch) == 1
    launch = sessions.at_launch[0]
    assert (launch["target"], launch["issue"], launch["worktree"]) == (
        before.target, before.issue, before.worktree)
    # An attach wake queues the dispatcher's own notice beside them.
    assert {message.id for message in launch["messages"]
            if message.actor != "dispatcher"} == {
        message.id for message in queued}
    assert all(not message.delivered_at for message in launch["messages"]), (
        "Queued messages must remain pending at the fresh launch boundary")
    for message in queued:
        assert message.text in launch["prompt"]
    assert sessions.continuations == []


@pytest.mark.parametrize("model", MODELS)
def test_deeply_nested_record_restarts_review_without_losing_messages(
        tmp_path, monkeypatch, model):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model)
    record = Path(c.state_dir) / f"session-{before.target}-{before.issue}"
    record.write_text("[" * 10000 + "]" * 10000)
    sessions = _observe_launch(c, d)

    launch, after = _pass(c, d, before)

    assert after.stage is Stage.REVIEW
    assert after.park == ""
    assert d.github.created_issues == []
    _assert_pending_at_launch(sessions, before, queued)
    _assert_delivered(c, before, queued, launch["prompt"])


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_fresh_restart_retains_nondefault_labels_and_board_effort(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, _ = _arrange(tmp_path, monkeypatch, model, kind, record)
    save(c.state_dir, replace(before, labels=("bug", "api"), effort=5))
    before = load(c.state_dir, before.target, before.issue)
    assert before.labels == ("bug", "api")
    assert before.effort == 5

    _, after = _pass(c, d, before)

    assert after.labels == ("bug", "api"), "Restart must retain board labels"
    assert after.effort == 5, "Restart must retain numeric board effort"


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_fresh_restart_delivers_messages_only_after_successful_launch(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model, kind, record)
    assert messages.undelivered(c.state_dir, before.target, before.issue) == list(queued)
    sessions = _observe_launch(c, d)

    launch, _ = _pass(c, d, before)

    _assert_pending_at_launch(sessions, before, queued)
    _assert_delivered(c, before, queued, launch["prompt"])


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("kind", KINDS)
def test_rejected_fresh_restart_keeps_queued_messages_pending(
        tmp_path, monkeypatch, model, record, kind):
    c, d, before, queued = _arrange(tmp_path, monkeypatch, model, kind, record)
    assert messages.undelivered(c.state_dir, before.target, before.issue) == list(queued)
    sessions = _observe_launch(c, d, reject=True)

    main.run_pass(c, d)

    assert sessions.launches == []
    assert load(c.state_dir, before.target, before.issue).stage is Stage.FAILED
    assert messages.undelivered(c.state_dir, before.target, before.issue) == list(queued), (
        "A failed fresh launch must leave queued messages pending")
    _assert_pending_at_launch(sessions, before, queued)
