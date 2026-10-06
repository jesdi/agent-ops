"""Additional stop-hook JSON regressions at the approved subprocess seam."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import dispatcher.main as main
from dispatcher.state import SessionRecord, Stage, has_waiting, load, read_session, save, write_session
from tests.test_main import deps, patch_events
from tests.test_session_resume_acceptance import CODEX_MODEL, RecordingSessions, _config
from tests.test_stop_hook_session_acceptance import (
    CLAUDE_ID,
    CHILD_IDS,
    ISSUE,
    ROOT_ID,
    TARGET,
    _notify,
    _rollout,
    _run,
    hook_fixture,
    served_hook,
)


def test_subagent_turn_leaves_live_root_review_unparked_after_dispatch(
        served_hook, tmp_path, monkeypatch):
    c = replace(_config(tmp_path, monkeypatch), state_dir=str(served_hook.state))
    patch_events(monkeypatch, [])
    task = load(c.state_dir, TARGET, ISSUE)
    save(c.state_dir, replace(
        task, stage=Stage.REVIEW, track="standard", picks={"review": CODEX_MODEL},
        updated_at=datetime.now(timezone.utc).isoformat()))
    (served_hook.worktree / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "review", "status": "working"}))
    original = SessionRecord(ROOT_ID, "review")
    write_session(c.state_dir, TARGET, ISSUE, original)
    _rollout(served_hook, ROOT_ID)
    _rollout(served_hook, CHILD_IDS[0], subagent=True)
    sessions = RecordingSessions(c.state_dir, alive={ISSUE})
    d = deps(sess=sessions)

    result = _run(served_hook, arguments=(_notify(CHILD_IDS[0]),))
    waiting_after_hook = has_waiting(c.state_dir, TARGET, ISSUE)
    main.run_pass(c, d)

    assert result.returncode == 0, result.stderr
    after = load(c.state_dir, TARGET, ISSUE)
    assert after.stage is Stage.REVIEW
    assert after.park == ""
    assert sessions.is_alive(TARGET, ISSUE)
    assert sessions.end_calls == []
    assert sessions.spawned == [] and sessions.resume_records == []
    assert read_session(c.state_dir, TARGET, ISSUE) == original
    assert not waiting_after_hook
    assert not has_waiting(c.state_dir, TARGET, ISSUE)


@pytest.mark.parametrize("target", ['portfolio"quoted', "portfolio\\backslash"])
def test_target_round_trips_through_hook_ping(served_hook, target):
    task = load(served_hook.state, TARGET, ISSUE)
    save(served_hook.state, replace(task, target=target))
    (served_hook.worktree / ".agent" / "task.json").write_text(
        json.dumps({"target": target, "issue": ISSUE})
    )

    result = _run(served_hook, stdin=json.dumps({"session_id": CLAUDE_ID}))

    assert result.returncode == 0, result.stderr
    assert has_waiting(served_hook.state, target, ISSUE)
    assert read_session(served_hook.state, target, ISSUE) == SessionRecord(CLAUDE_ID, "implement")


@pytest.mark.parametrize("source", ["hook", "stage"])
def test_unusable_deep_json_still_reports_waiting(served_hook, source):
    deeply_nested = "[" * 2000 + "]" * 2000
    stdin = deeply_nested
    expected_session = None
    if source == "stage":
        (served_hook.worktree / ".agent" / "stage.json").write_text(deeply_nested)
        stdin = json.dumps({"session_id": CLAUDE_ID})
        expected_session = SessionRecord(CLAUDE_ID, "implement")

    result = _run(served_hook, stdin=stdin)

    assert result.returncode == 0, result.stderr
    assert has_waiting(served_hook.state, TARGET, ISSUE)
    assert read_session(served_hook.state, TARGET, ISSUE) == expected_session
