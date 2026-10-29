"""T01 acceptance: a turn end records only its task's conversation.

Exercise the approved ping seam and public state APIs. Record-file fixtures
use the filename specified in specs/session-isolation/design.md.
"""

import json

import pytest

from dispatcher.state import (
    SessionRecord,
    Stage,
    TaskState,
    clear_session,
    clear_turn_markers,
    has_waiting,
    mark_background,
    mark_waiting,
    read_background,
    read_session,
    save,
)
from dispatcher.waitd import handle_ping


REPOSITORY = "portfolio_eval"
ISSUE = 370
FIRST_ID = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
SECOND_ID = "9d8c7b6a-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
BACKGROUND_TASK = {"id": "gate-1", "type": "shell", "command": "make crap-gate"}
INVALID_PINGS = [
    pytest.param({}, id="missing-id"),
    pytest.param({"session_id": ""}, id="empty-id"),
    pytest.param({"session_id": None}, id="null-id"),
    pytest.param({"session_id": 370}, id="integer-id"),
    pytest.param({"session_id": True}, id="boolean-id"),
    pytest.param({"session_id": [FIRST_ID]}, id="list-id"),
    pytest.param({"session_id": {"id": FIRST_ID}}, id="object-id"),
    pytest.param({"session_id": SECOND_ID, "repository": ""}, id="empty-repository"),
]


def _task(state_dir, stage=Stage.IMPLEMENT, repository=REPOSITORY, issue=ISSUE):
    save(state_dir, TaskState(
        issue=issue,
        repository=repository,
        stage=stage,
        slot=0,
        worktree=f"/home/agent/repos/{repository}.worktrees/task-{issue}",
        branch=f"agent/task-{issue}",
        title="Record the task's own conversation",
        updated_at="2026-10-06T09:00:00+00:00",
    ))


def _ping(state_dir, **fields):
    body = {"issue": ISSUE, "repository": REPOSITORY, **fields}
    handle_ping(json.dumps(body).encode(), state_dir)


def test_turn_end_records_the_session_id_and_current_stage(tmp_path):
    _task(tmp_path)

    _ping(tmp_path, session_id=FIRST_ID)

    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")


def test_awaiting_spec_review_records_the_continued_spec_stage(tmp_path):
    _task(tmp_path, stage=Stage.AWAITING_PLAN_REVIEW)

    _ping(tmp_path, session_id=FIRST_ID)

    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "plan")


def test_later_turn_end_replaces_the_recorded_conversation(tmp_path):
    _task(tmp_path)
    _ping(tmp_path, session_id=FIRST_ID)
    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")

    _ping(tmp_path, session_id=SECOND_ID)

    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(SECOND_ID, "implement")


@pytest.mark.parametrize("fields", INVALID_PINGS)
def test_unusable_identity_neither_creates_nor_replaces_a_record(tmp_path, fields):
    _task(tmp_path)
    _ping(tmp_path, **fields)
    assert read_session(tmp_path, REPOSITORY, ISSUE) is None
    assert read_session(tmp_path, "", ISSUE) is None

    _ping(tmp_path, session_id=FIRST_ID)
    original = SessionRecord(FIRST_ID, "implement")
    assert read_session(tmp_path, REPOSITORY, ISSUE) == original

    _ping(tmp_path, **fields)

    assert read_session(tmp_path, REPOSITORY, ISSUE) == original
    assert read_session(tmp_path, "", ISSUE) is None


def test_turn_end_for_a_task_without_state_creates_no_record(tmp_path):
    _task(tmp_path, issue=ISSUE + 1)
    _task(tmp_path, repository="agent_ops")

    _ping(tmp_path, session_id=FIRST_ID)

    assert read_session(tmp_path, REPOSITORY, ISSUE) is None


@pytest.mark.parametrize("fields,stage", [
    pytest.param({"session_id": FIRST_ID}, Stage.IMPLEMENT, id="implement"),
    pytest.param({"session_id": FIRST_ID}, Stage.AWAITING_PLAN_REVIEW, id="plan-review"),
    pytest.param({"session_id": SECOND_ID}, Stage.IMPLEMENT, id="later-id"),
    *[pytest.param(p.values[0], Stage.IMPLEMENT, id=p.id) for p in INVALID_PINGS],
    pytest.param({"session_id": FIRST_ID}, None, id="missing-state"),
])
@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]], ids=["waiting", "background"])
@pytest.mark.parametrize("prior_marker", ["waiting", "background"])
def test_session_identity_keeps_existing_turn_marker_behavior(
        tmp_path, fields, stage, background_tasks, prior_marker):
    with_identity = tmp_path / "with-identity"
    without_identity = tmp_path / "without-identity"
    for state_dir in (with_identity, without_identity):
        state_dir.mkdir()
        if stage is not None:
            _task(state_dir, stage=stage)
        if prior_marker == "waiting":
            mark_waiting(state_dir, REPOSITORY, ISSUE)
        else:
            mark_background(state_dir, REPOSITORY, ISSUE, [BACKGROUND_TASK], now=1000)

    payload = {**fields, "background_tasks": background_tasks}
    _ping(with_identity, **payload)
    _ping(without_identity, **{k: v for k, v in payload.items() if k != "session_id"})

    expects_background = bool(background_tasks) and payload.get("repository", REPOSITORY) != ""
    for state_dir in (with_identity, without_identity):
        assert has_waiting(state_dir, REPOSITORY, ISSUE) is (not expects_background)
        background = read_background(state_dir, REPOSITORY, ISSUE)
        if expects_background:
            assert background is not None
            assert background.tasks == ("gate-1",)
            if prior_marker == "background":
                assert background.since == 1000
        elif payload.get("repository", REPOSITORY) == "" and prior_marker == "background":
            # Legacy waiting leaves a different repository's background marker alone.
            assert background is not None
            assert background.tasks == ("gate-1",)
            assert background.since == 1000
        else:
            assert background is None

    if (expects_background and stage is not None
            and isinstance(fields.get("session_id"), str) and fields["session_id"]):
        recorded_stage = "plan" if stage is Stage.AWAITING_PLAN_REVIEW else "implement"
        assert read_session(with_identity, REPOSITORY, ISSUE) == SessionRecord(
            fields["session_id"], recorded_stage)


@pytest.mark.parametrize("contents", [
    '{"session_id":',
    '{"stage": "implement"}',
    '{"session_id": "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"}',
], ids=["truncated-json", "missing-id", "missing-stage"])
def test_unreadable_or_incomplete_session_record_reads_as_absent(tmp_path, contents):
    (tmp_path / f"session-{REPOSITORY}-{ISSUE}").write_text(contents)

    assert read_session(tmp_path, REPOSITORY, ISSUE) is None


def test_clear_session_removes_only_the_record_and_is_idempotent(tmp_path):
    _task(tmp_path)
    _ping(tmp_path, session_id=FIRST_ID, background_tasks=[BACKGROUND_TASK])
    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")
    background = read_background(tmp_path, REPOSITORY, ISSUE)
    assert background is not None

    clear_session(tmp_path, REPOSITORY, ISSUE)

    assert read_session(tmp_path, REPOSITORY, ISSUE) is None
    assert read_background(tmp_path, REPOSITORY, ISSUE) == background
    clear_session(tmp_path, REPOSITORY, ISSUE)
    assert read_session(tmp_path, REPOSITORY, ISSUE) is None


def test_session_records_are_isolated_by_both_repository_and_issue(tmp_path):
    _task(tmp_path)
    _task(tmp_path, issue=ISSUE + 1)
    _task(tmp_path, repository="agent_ops")
    _ping(tmp_path, session_id=FIRST_ID)
    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")

    assert read_session(tmp_path, REPOSITORY, ISSUE + 1) is None
    assert read_session(tmp_path, "agent_ops", ISSUE) is None
    _ping(tmp_path, repository="agent_ops", session_id=SECOND_ID)
    assert read_session(tmp_path, "agent_ops", ISSUE) == SessionRecord(SECOND_ID, "implement")
    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")

    clear_session(tmp_path, "agent_ops", ISSUE)
    clear_turn_markers(tmp_path, "agent_ops", ISSUE)
    assert read_session(tmp_path, REPOSITORY, ISSUE) == SessionRecord(FIRST_ID, "implement")
