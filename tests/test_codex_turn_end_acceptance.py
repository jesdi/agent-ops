"""T02 acceptance: only the task's own Codex root turn records a session.

All outcomes are observed through the approved ping and public state APIs.
Rollouts are synthetic Codex-owned inputs; no runtime CLI is invoked.
"""

import json

import pytest

from dispatcher.state import (
    SessionRecord,
    Stage,
    TaskState,
    clear_turn_markers,
    has_waiting,
    mark_background,
    mark_waiting,
    read_background,
    read_session,
    save,
)
from dispatcher.waitd import handle_ping


TARGET = "portfolio_eval"
ISSUE = 370
ROOT_ID = "01a11028-9f0c-75f3-8b77-a5b5e6dfb884"
OLD_ID = "01a10000-9f0c-75f3-8b77-a5b5e6dfb884"
SUBAGENT_IDS = (
    "01a11032-5278-75f3-8b77-a5b5e6dfb884",
    "01a11033-5278-75f3-8b77-a5b5e6dfb884",
    "01a11034-5278-75f3-8b77-a5b5e6dfb884",
)
BACKGROUND_TASK = {"id": "gate-1", "type": "shell", "command": "make crap-gate"}
SUBAGENT_SOURCE = {"subagent": {"parent_thread_id": ROOT_ID, "thread_spawn_depth": 1}}


def _worktree(issue=ISSUE):
    return f"/home/agent/repos/{TARGET}.worktrees/task-{issue}"


def _task(state_dir, issue=ISSUE, stage=Stage.REVIEW):
    save(state_dir, TaskState(
        issue=issue,
        target=TARGET,
        stage=stage,
        slot=0,
        worktree=_worktree(issue),
        branch=f"agent/task-{issue}",
        title="Keep each task's Codex conversation isolated",
        updated_at="2026-10-06T09:00:00+00:00",
    ))


def _ping(state_dir, issue=ISSUE, **fields):
    handle_ping(json.dumps({"issue": issue, "target": TARGET, **fields}).encode(), state_dir)


def _metadata(session_id=ROOT_ID, cwd=None, source="cli"):
    return {"type": "session_meta", "payload": {
        "id": session_id,
        "cwd": _worktree() if cwd is None else cwd,
        "source": source,
    }}


def _rollout(state_dir, session_id, first_line):
    directory = state_dir / "codex-home" / "sessions" / "2026" / "10" / "06"
    directory.mkdir(parents=True, exist_ok=True)
    contents = first_line if isinstance(first_line, str) else json.dumps(first_line) + "\n"
    (directory / f"rollout-2026-10-06T08-16-00-{session_id}.jsonl").write_text(contents)


def _seed_record(state_dir, session_id=OLD_ID, issue=ISSUE):
    _ping(state_dir, issue=issue, session_id=session_id)
    record = read_session(state_dir, TARGET, issue)
    assert record is not None and record.session_id == session_id
    clear_turn_markers(state_dir, TARGET, issue)
    return record


@pytest.mark.parametrize("source", ["cli", "exec"])
@pytest.mark.parametrize("stage,recorded_stage", [
    pytest.param(Stage.REVIEW, "review", id="review"),
    pytest.param(Stage.AWAITING_PLAN_REVIEW, "plan", id="continued-plan"),
])
@pytest.mark.parametrize("existing_record", [False, True], ids=["absent", "existing"])
def test_own_root_turn_records_its_id_and_writes_waiting(
        tmp_path, source, stage, recorded_stage, existing_record):
    _task(tmp_path, stage=stage)
    if existing_record:
        _seed_record(tmp_path)
    mark_background(tmp_path, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)
    _rollout(tmp_path, ROOT_ID, _metadata(source=source))

    _ping(tmp_path, runtime="codex", session_id=ROOT_ID)

    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(ROOT_ID, recorded_stage)
    assert has_waiting(tmp_path, TARGET, ISSUE)
    assert read_background(tmp_path, TARGET, ISSUE) is None


@pytest.mark.parametrize("existing_record", [False, True], ids=["absent", "existing"])
@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]], ids=["plain", "background"])
def test_subagent_turn_creates_no_markers_and_preserves_the_session_record(
        tmp_path, existing_record, background_tasks):
    _task(tmp_path)
    expected = _seed_record(tmp_path) if existing_record else None
    subagent_id = SUBAGENT_IDS[0]
    _rollout(tmp_path, subagent_id, _metadata(subagent_id, source=SUBAGENT_SOURCE))

    _ping(tmp_path, runtime="codex", session_id=subagent_id, background_tasks=background_tasks)

    assert read_session(tmp_path, TARGET, ISSUE) == expected
    assert not has_waiting(tmp_path, TARGET, ISSUE)
    assert read_background(tmp_path, TARGET, ISSUE) is None


@pytest.mark.parametrize("prior_markers", ["waiting", "background"])
@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]], ids=["plain", "background"])
def test_subagent_turn_preserves_existing_waiting_and_background_markers(
        tmp_path, prior_markers, background_tasks):
    _task(tmp_path)
    original = _seed_record(tmp_path)
    if prior_markers == "background":
        mark_background(tmp_path, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)
    if prior_markers == "waiting":
        mark_waiting(tmp_path, TARGET, ISSUE)
    waiting = has_waiting(tmp_path, TARGET, ISSUE)
    background = read_background(tmp_path, TARGET, ISSUE)
    assert waiting is (prior_markers == "waiting")
    assert (background is not None) is (prior_markers == "background")
    subagent_id = SUBAGENT_IDS[0]
    _rollout(tmp_path, subagent_id, _metadata(subagent_id, source=SUBAGENT_SOURCE))

    _ping(tmp_path, runtime="codex", session_id=subagent_id, background_tasks=background_tasks)

    assert has_waiting(tmp_path, TARGET, ISSUE) is waiting
    assert read_background(tmp_path, TARGET, ISSUE) == background
    assert read_session(tmp_path, TARGET, ISSUE) == original


@pytest.mark.parametrize("existing_record", [False, True], ids=["absent", "existing"])
def test_three_subagent_turns_are_ignored_until_the_root_turn_ends(tmp_path, existing_record):
    _task(tmp_path)
    original = _seed_record(tmp_path, ROOT_ID) if existing_record else None
    _rollout(tmp_path, ROOT_ID, _metadata())
    for subagent_id in SUBAGENT_IDS:
        _rollout(tmp_path, subagent_id, _metadata(subagent_id, source=SUBAGENT_SOURCE))

        _ping(tmp_path, runtime="codex", session_id=subagent_id)

        assert not has_waiting(tmp_path, TARGET, ISSUE)
        assert read_background(tmp_path, TARGET, ISSUE) is None
        assert read_session(tmp_path, TARGET, ISSUE) == original

    _ping(tmp_path, runtime="codex", session_id=ROOT_ID)

    assert has_waiting(tmp_path, TARGET, ISSUE)
    assert read_background(tmp_path, TARGET, ISSUE) is None
    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(ROOT_ID, "review")


@pytest.mark.parametrize("existing_record", [False, True], ids=["absent", "existing"])
def test_another_tasks_root_turn_parks_without_replacing_either_record(tmp_path, existing_record):
    _task(tmp_path)
    _task(tmp_path, issue=ISSUE + 1)
    expected = _seed_record(tmp_path) if existing_record else None
    other_record = _seed_record(tmp_path, ROOT_ID, issue=ISSUE + 1)
    _rollout(tmp_path, ROOT_ID, _metadata(cwd=_worktree(ISSUE + 1)))

    _ping(tmp_path, runtime="codex", session_id=ROOT_ID)

    assert has_waiting(tmp_path, TARGET, ISSUE)
    assert read_background(tmp_path, TARGET, ISSUE) is None
    assert read_session(tmp_path, TARGET, ISSUE) == expected
    assert read_session(tmp_path, TARGET, ISSUE + 1) == other_record
    assert not has_waiting(tmp_path, TARGET, ISSUE + 1)


UNIDENTIFIED_CASES = [
    "missing-session-id", "empty-session-id", "non-string-session-id", "no-rollout",
    "empty-file", "invalid-first-line", "valid-metadata-on-second-line",
    "non-object-first-line", "wrong-type", "missing-type", "missing-payload",
    "non-object-payload", "missing-source", "non-string-source", "unrecognized-object-source",
    "missing-cwd", "null-cwd", "non-string-cwd", "missing-payload-id", "mismatched-payload-id",
]


def _unidentified_rollout(state_dir, case):
    fields = {"runtime": "codex", "session_id": ROOT_ID}
    if case == "missing-session-id":
        fields.pop("session_id")
    elif case == "empty-session-id":
        fields["session_id"] = ""
    elif case == "non-string-session-id":
        fields["session_id"] = 370
    elif case != "no-rollout":
        metadata = _metadata()
        payload = metadata["payload"]
        if case == "empty-file":
            metadata = ""
        elif case == "invalid-first-line":
            metadata = "{not json\n"
        elif case == "valid-metadata-on-second-line":
            metadata = "{not json\n" + json.dumps(metadata) + "\n"
        elif case == "non-object-first-line":
            metadata = []
        elif case == "wrong-type":
            metadata["type"] = "event_msg"
        elif case == "missing-type":
            metadata.pop("type")
        elif case == "missing-payload":
            metadata.pop("payload")
        elif case == "non-object-payload":
            metadata["payload"] = []
        elif case == "missing-source":
            payload.pop("source")
        elif case == "non-string-source":
            payload["source"] = 370
        elif case == "unrecognized-object-source":
            payload["source"] = {"unexpected": "cli"}
        elif case == "missing-cwd":
            payload.pop("cwd")
        elif case == "null-cwd":
            payload["cwd"] = None
        elif case == "non-string-cwd":
            payload["cwd"] = [_worktree()]
        elif case == "missing-payload-id":
            payload.pop("id")
        elif case == "mismatched-payload-id":
            payload["id"] = OLD_ID
        _rollout(state_dir, ROOT_ID, metadata)
    return fields


@pytest.mark.parametrize("case", UNIDENTIFIED_CASES)
@pytest.mark.parametrize("existing_record", [False, True], ids=["absent", "existing"])
def test_unidentified_codex_turn_parks_without_creating_or_replacing_a_record(
        tmp_path, case, existing_record):
    _task(tmp_path)
    expected = _seed_record(tmp_path) if existing_record else None
    mark_background(tmp_path, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)
    fields = _unidentified_rollout(tmp_path, case)

    _ping(tmp_path, **fields)

    assert has_waiting(tmp_path, TARGET, ISSUE)
    assert read_background(tmp_path, TARGET, ISSUE) is None
    assert read_session(tmp_path, TARGET, ISSUE) == expected


@pytest.mark.parametrize("rollout_kind", ["subagent", "other-worktree", "invalid-first-line"])
@pytest.mark.parametrize("background_tasks", [None, [BACKGROUND_TASK]], ids=["waiting", "background"])
def test_no_runtime_ping_keeps_ticket_one_behavior_regardless_of_rollout(
        tmp_path, rollout_kind, background_tasks):
    _task(tmp_path)
    _seed_record(tmp_path)
    if rollout_kind == "subagent":
        metadata = _metadata(source=SUBAGENT_SOURCE)
    elif rollout_kind == "other-worktree":
        metadata = _metadata(cwd=_worktree(ISSUE + 1))
    else:
        metadata = "{not json\n"
    _rollout(tmp_path, ROOT_ID, metadata)
    mark_background(tmp_path, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)

    _ping(tmp_path, session_id=ROOT_ID, background_tasks=background_tasks)

    assert read_session(tmp_path, TARGET, ISSUE) == SessionRecord(ROOT_ID, "review")
    assert has_waiting(tmp_path, TARGET, ISSUE) is (background_tasks is None)
    background = read_background(tmp_path, TARGET, ISSUE)
    if background_tasks is None:
        assert background is None
    else:
        assert background is not None
        assert background.tasks == ("gate-1",)
        assert background.since == 1000
