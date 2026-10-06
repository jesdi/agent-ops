"""Additional stop-hook JSON regressions at the approved subprocess seam."""

import json
from dataclasses import replace

import pytest

from dispatcher.state import SessionRecord, has_waiting, load, read_session, save
from tests.test_stop_hook_session_acceptance import (
    CLAUDE_ID,
    ISSUE,
    TARGET,
    _run,
    hook_fixture,
    served_hook,
)


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
