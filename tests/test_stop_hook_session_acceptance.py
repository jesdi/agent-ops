"""T03 acceptance: actual stop hook forwards a root conversation to waitd.

Locked public seam: hook subprocess, actual served waitd, public state reads.
Codex rollout fixtures are synthetic; no logged-in runtime is invoked.
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest

from dispatcher.state import (
    SessionRecord,
    Stage,
    TaskState,
    has_waiting,
    mark_background,
    read_background,
    read_session,
    save,
    write_session,
)
from dispatcher.waitd import sock_path


REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "stop-hook.sh"
TARGET = "portfolio_eval"
ISSUE = 370
CLAUDE_ID = "7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d"
ROOT_ID = "01a11028-ed88-72c0-a20f-b801e1eff4a6"
CHILD_IDS = (
    "01a11032-5278-72c0-a20f-b801e1eff4a6",
    "01a11033-5278-72c0-a20f-b801e1eff4a6",
    "01a11034-5278-72c0-a20f-b801e1eff4a6",
)
BACKGROUND_TASK = {"id": "gate-1", "type": "shell", "command": "make crap-gate"}
SPECIAL_IDS = ('conversation"quoted', "conversation\\backslash")

INVALID_STDIN = (
    pytest.param("", id="empty"),
    pytest.param("garbage", id="not-json"),
    pytest.param("{}", id="missing-id"),
    pytest.param(json.dumps({"session_id": ""}), id="empty-id"),
    pytest.param(json.dumps({"session_id": None}), id="null-id"),
    pytest.param(json.dumps({"session_id": 370}), id="integer-id"),
    pytest.param(json.dumps({"session_id": True}), id="boolean-id"),
    pytest.param(json.dumps({"session_id": [CLAUDE_ID]}), id="list-id"),
    pytest.param(json.dumps({"session_id": {"id": CLAUDE_ID}}), id="object-id"),
    pytest.param("null", id="null-input"),
    pytest.param("[]", id="list-input"),
    pytest.param("false", id="boolean-input"),
    pytest.param("370", id="integer-input"),
    pytest.param('"text"', id="string-input"),
)
INVALID_ARGUMENTS = (
    pytest.param("garbage", id="not-json"),
    pytest.param("", id="empty"),
    pytest.param("{}", id="missing-id"),
    pytest.param('{"type":"agent-turn-complete"}', id="notification-without-id"),
    pytest.param(json.dumps({"thread-id": ""}), id="empty-id"),
    pytest.param(json.dumps({"thread-id": None}), id="null-id"),
    pytest.param(json.dumps({"thread-id": 370}), id="integer-id"),
    pytest.param(json.dumps({"thread-id": True}), id="boolean-id"),
    pytest.param(json.dumps({"thread-id": [ROOT_ID]}), id="list-id"),
    pytest.param(json.dumps({"thread-id": {"id": ROOT_ID}}), id="object-id"),
    pytest.param("null", id="null-input"),
    pytest.param("[]", id="list-input"),
    pytest.param("false", id="boolean-input"),
)


def _notify(thread_id):
    return json.dumps({"type": "agent-turn-complete", "thread-id": thread_id})


@dataclass(frozen=True)
class HookFixture:
    state: Path
    worktree: Path
    hook: Path

    @property
    def environment(self):
        return {"PATH": "/usr/bin:/bin", "AGENT_OPS_STATE_DIR": str(self.state)}


@pytest.fixture
def hook_fixture():
    # macOS limits an AF_UNIX address to about 104 bytes. pytest's default
    # directory can exceed that limit before the socket basename is appended.
    with tempfile.TemporaryDirectory(prefix="t03_", dir="/tmp") as temporary:
        base = Path(temporary).resolve()
        state = base / "state"
        agent = base / "wt" / ".agent"
        state.mkdir()
        agent.mkdir(parents=True)
        (agent / "task.json").write_text(json.dumps({"target": TARGET, "issue": ISSUE}))
        shutil.copyfile(HOOK, agent / "stop-hook.sh")
        fixture = HookFixture(state, agent.parent, agent / "stop-hook.sh")
        save(state, TaskState(
            issue=ISSUE, target=TARGET, stage=Stage.IMPLEMENT, slot=0,
            worktree=str(fixture.worktree), branch=f"agent/task-{ISSUE}",
            title="Record the task's own conversation",
            updated_at="2026-10-06T09:00:00+00:00",
        ))
        yield fixture


@pytest.fixture
def served_hook(hook_fixture):
    # Run the public server in a separate process so cleanup stops the server
    # before the socket's temporary directory is removed.
    program = (
        "import sys; from dispatcher.waitd import serve, sock_path; "
        "serve(sock_path(sys.argv[1]), sys.argv[1])"
    )
    server = subprocess.Popen(
        [sys.executable, "-c", program, str(hook_fixture.state)],
        cwd=REPO, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.fail(f"waitd exited before listening: {server.stderr.read()}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(sock_path(hook_fixture.state)))
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    time.sleep(0.02)
        else:
            pytest.fail("waitd never started accepting connections")
        yield hook_fixture
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        server.stderr.close()


def _working(fixture):
    (fixture.worktree / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"})
    )


def _rollout(fixture, thread_id, *, subagent=False):
    sessions = fixture.state / "codex-home" / "sessions" / "2026" / "10" / "06"
    sessions.mkdir(parents=True, exist_ok=True)
    source = ({"subagent": {"thread_spawn": {
        "parent_thread_id": ROOT_ID, "depth": 1,
    }}} if subagent else "cli")
    metadata = {
        "timestamp": "2026-10-06T09:00:00.000Z", "type": "session_meta",
        "payload": {"id": thread_id, "cwd": str(fixture.worktree), "source": source},
    }
    (sessions / f"rollout-2026-10-06T09-00-00-{thread_id}.jsonl").write_text(
        json.dumps(metadata) + "\n"
    )


def _run(fixture, *, stdin="", arguments=()):
    return subprocess.run(
        ["bash", str(fixture.hook), *arguments], input=stdin, text=True,
        cwd=fixture.worktree, env=fixture.environment, timeout=10,
        capture_output=True,
    )


def _run_with_open_stdin(fixture, argument):
    # No bytes or EOF reach stdin until after the process has exited. Reading
    # stdin in argument mode therefore causes a timeout, rather than passing
    # because the test's pipe was already closed.
    hook = subprocess.Popen(
        ["bash", str(fixture.hook), argument], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=fixture.worktree, env=fixture.environment,
    )
    try:
        return hook.wait(timeout=5)
    finally:
        if hook.poll() is None:
            hook.kill()
            hook.wait(timeout=5)
        hook.stdin.close()


def _assert_waiting(fixture):
    assert has_waiting(fixture.state, TARGET, ISSUE)
    assert read_background(fixture.state, TARGET, ISSUE) is None


def test_claude_implement_turn_records_stdin_session_and_waits(served_hook):
    result = _run(served_hook, stdin=json.dumps({"session_id": CLAUDE_ID}))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(CLAUDE_ID, "implement")
    _assert_waiting(served_hook)


def test_claude_working_turn_records_session_and_background_without_waiting(served_hook):
    _working(served_hook)
    result = _run(served_hook, stdin=json.dumps({
        "session_id": CLAUDE_ID, "background_tasks": [BACKGROUND_TASK],
    }))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(CLAUDE_ID, "implement")
    background = read_background(served_hook.state, TARGET, ISSUE)
    assert background is not None
    assert background.tasks == ("gate-1",)
    assert not has_waiting(served_hook.state, TARGET, ISSUE)


@pytest.mark.parametrize("stdin", INVALID_STDIN)
def test_claude_unusable_stdin_waits_without_recording(served_hook, stdin):
    result = _run(served_hook, stdin=stdin)

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) is None
    _assert_waiting(served_hook)


def test_codex_root_argument_records_its_thread_and_waits(served_hook):
    _rollout(served_hook, ROOT_ID)
    result = _run(served_hook, arguments=(_notify(ROOT_ID),))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(ROOT_ID, "implement")
    _assert_waiting(served_hook)


@pytest.mark.parametrize("prior_background", [False, True], ids=["no-marker", "background"])
def test_codex_subagent_argument_preserves_root_record_and_markers(served_hook, prior_background):
    original = SessionRecord(ROOT_ID, "implement")
    write_session(served_hook.state, TARGET, ISSUE, original)
    if prior_background:
        mark_background(served_hook.state, TARGET, ISSUE, [BACKGROUND_TASK], now=1000)
    background = read_background(served_hook.state, TARGET, ISSUE)
    _rollout(served_hook, CHILD_IDS[0], subagent=True)

    result = _run(served_hook, arguments=(_notify(CHILD_IDS[0]),))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) == original
    assert read_background(served_hook.state, TARGET, ISSUE) == background
    assert not has_waiting(served_hook.state, TARGET, ISSUE)


def test_codex_three_subagents_wait_only_after_root_turn_ends(served_hook):
    _rollout(served_hook, ROOT_ID)
    for thread_id in CHILD_IDS:
        _rollout(served_hook, thread_id, subagent=True)
        result = _run(served_hook, arguments=(_notify(thread_id),))
        assert result.returncode == 0, result.stderr
        assert not has_waiting(served_hook.state, TARGET, ISSUE)
        assert read_background(served_hook.state, TARGET, ISSUE) is None
        assert read_session(served_hook.state, TARGET, ISSUE) is None

    result = _run(served_hook, arguments=(_notify(ROOT_ID),))

    assert result.returncode == 0, result.stderr
    _assert_waiting(served_hook)
    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(ROOT_ID, "implement")


@pytest.mark.parametrize("argument", INVALID_ARGUMENTS)
def test_codex_unusable_argument_waits_without_recording(served_hook, argument):
    result = _run(served_hook, arguments=(argument,))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) is None
    _assert_waiting(served_hook)


def test_codex_argument_exits_while_stdin_pipe_remains_open(served_hook):
    _rollout(served_hook, ROOT_ID)

    assert _run_with_open_stdin(served_hook, _notify(ROOT_ID)) == 0

    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(ROOT_ID, "implement")
    _assert_waiting(served_hook)


def test_codex_argument_wins_over_competing_claude_stdin(served_hook):
    _working(served_hook)
    _rollout(served_hook, ROOT_ID)
    result = _run(served_hook, arguments=(_notify(ROOT_ID),), stdin=json.dumps({
        "session_id": CLAUDE_ID, "background_tasks": [BACKGROUND_TASK],
    }))

    assert result.returncode == 0, result.stderr
    assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(ROOT_ID, "implement")
    _assert_waiting(served_hook)


@pytest.mark.parametrize("session_id", SPECIAL_IDS, ids=["quote", "backslash"])
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_quoted_or_backslashed_id_does_not_corrupt_ping(served_hook, session_id, runtime):
    if runtime == "codex":
        result = _run(served_hook, arguments=(_notify(session_id),))
    else:
        result = _run(served_hook, stdin=json.dumps({"session_id": session_id}))

    assert result.returncode == 0, result.stderr
    _assert_waiting(served_hook)
    if runtime == "claude":
        assert read_session(served_hook.state, TARGET, ISSUE) == SessionRecord(session_id, "implement")


# Each down-server case uses the same input shape exercised above. Three
# different child IDs also cover every notify in the subagent/root sequence.
DOWN_CASES = (
    pytest.param((), json.dumps({"session_id": CLAUDE_ID}), False, False, id="claude-valid"),
    pytest.param((), json.dumps({"session_id": CLAUDE_ID, "background_tasks": [BACKGROUND_TASK]}),
                 True, False, id="claude-background"),
    *[pytest.param((), p.values[0], False, False, id=f"claude-{p.id}") for p in INVALID_STDIN],
    pytest.param((_notify(ROOT_ID),), "", False, False, id="codex-root"),
    *[pytest.param((_notify(thread_id),), "", False, False, id=f"codex-subagent-{index}")
      for index, thread_id in enumerate(CHILD_IDS, start=1)],
    *[pytest.param((p.values[0],), "", False, False, id=f"codex-{p.id}") for p in INVALID_ARGUMENTS],
    pytest.param((_notify(ROOT_ID),), json.dumps({"session_id": CLAUDE_ID,
                 "background_tasks": [BACKGROUND_TASK]}), True, False, id="argument-wins"),
    pytest.param((_notify(ROOT_ID),), "", False, True, id="open-stdin"),
    *[pytest.param((), json.dumps({"session_id": session_id}), False, False,
                  id=f"claude-special-{index}") for index, session_id in enumerate(SPECIAL_IDS)],
    *[pytest.param((_notify(session_id),), "", False, False,
                  id=f"codex-special-{index}") for index, session_id in enumerate(SPECIAL_IDS)],
)


@pytest.mark.parametrize("arguments,stdin,working,open_stdin", DOWN_CASES)
def test_waitd_down_always_exits_zero(hook_fixture, arguments, stdin, working, open_stdin):
    assert not sock_path(hook_fixture.state).exists()
    if working:
        _working(hook_fixture)
    _rollout(hook_fixture, ROOT_ID)
    for thread_id in CHILD_IDS:
        _rollout(hook_fixture, thread_id, subagent=True)

    if open_stdin:
        assert _run_with_open_stdin(hook_fixture, arguments[0]) == 0
    else:
        result = _run(hook_fixture, arguments=arguments, stdin=stdin)
        assert result.returncode == 0, result.stderr
