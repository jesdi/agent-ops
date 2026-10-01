"""Acceptance: the Stop hook reports background work (ticket 02).

The real hook runs as a subprocess against a served waitd; outcome is read
through state.read_background / has_waiting and the exit code."""
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher.waitd import serve, sock_path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "stop-hook.sh"
T, N = "agent_ops", 329
SHELL = {"id": "b1", "type": "shell", "command": "make crap-gate"}
AGENT = {"id": "a1", "type": "agent", "description": "review"}
WORKING = {"stage": "review", "status": "working"}


def _listening(sock):
    for _ in range(100):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(sock))
            return
        except (ConnectionRefusedError, FileNotFoundError):
            threading.Event().wait(0.05)
        finally:
            s.close()
    pytest.fail("waitd never started accepting connections")


@pytest.fixture
def env():
    d = Path(tempfile.mkdtemp(prefix="w_"))
    threading.Thread(target=serve, args=(sock_path(d), d), daemon=True).start()
    _listening(sock_path(d))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run_hook(state, stage=WORKING, stdin="", args=(), target=T, **kw):
    """stage: dict -> stage.json, str -> raw stage.json, None -> absent."""
    agent = state / "wt" / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    task = {"issue": N} | ({"target": target} if target else {})
    (agent / "task.json").write_text(json.dumps(task))
    sj = agent / "stage.json"
    sj.unlink(missing_ok=True)
    if stage is not None:
        sj.write_text(stage if isinstance(stage, str) else json.dumps(stage))
    shutil.copy(HOOK, agent / "stop-hook.sh")
    return subprocess.run(
        ["bash", str(agent / "stop-hook.sh"), *args], input=stdin,
        text=True, timeout=30, capture_output=True,
        env={"PATH": "/usr/bin:/bin", "AGENT_OPS_STATE_DIR": str(state)}, **kw)


def bg_input(*tasks):
    return json.dumps({"background_tasks": list(tasks)})


def assert_background(state):
    from dispatcher.state import has_waiting, read_background
    assert read_background(state, T, N) is not None
    assert not has_waiting(state, T, N)


def assert_waiting(state):
    from dispatcher.state import has_waiting, read_background
    assert has_waiting(state, T, N)
    assert read_background(state, T, N) is None


@pytest.mark.parametrize("task", [SHELL, AGENT], ids=["shell", "agent"])
def test_working_with_background_task_reports_background(env, task):
    assert run_hook(env, stdin=bg_input(task)).returncode == 0
    assert_background(env)


def test_working_with_empty_background_list_waits(env):
    assert run_hook(env, stdin=bg_input()).returncode == 0
    assert_waiting(env)


@pytest.mark.parametrize("status", [
    "blocked", "awaiting-answers", "awaiting-ci", "awaiting-review", "done"])
def test_non_working_status_waits_despite_background(env, status):
    r = run_hook(env, stage={"stage": "review", "status": status},
                 stdin=bg_input(SHELL))
    assert r.returncode == 0
    assert_waiting(env)


@pytest.mark.parametrize("stage", [None, "{not json"], ids=["absent", "corrupt"])
def test_missing_or_corrupt_stage_waits_despite_background(env, stage):
    assert run_hook(env, stage=stage, stdin=bg_input(SHELL)).returncode == 0
    assert_waiting(env)


@pytest.mark.parametrize("stdin", ["", "garbage", "{}"])
def test_unusable_hook_input_waits(env, stdin):
    assert run_hook(env, stdin=stdin).returncode == 0
    assert_waiting(env)


def test_codex_argument_pings_waiting(env):
    r = run_hook(env, args=['{"type": "agent-turn-complete"}'], stdin="")
    assert r.returncode == 0
    assert_waiting(env)


def test_codex_never_reads_stdin_and_never_reports_background(env):
    # stdin stays open and unwritten: a hook that reads it hangs -> timeout.
    agent = env / "wt" / ".agent"
    run_hook(env, stdin="")  # lay out .agent, then re-run with an open pipe
    from dispatcher.state import clear_waiting
    clear_waiting(env, T, N)
    p = subprocess.Popen(
        ["bash", str(agent / "stop-hook.sh"), '{"type": "agent-turn-complete"}'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "AGENT_OPS_STATE_DIR": str(env)})
    try:
        assert p.wait(timeout=10) == 0
    finally:
        p.kill()
        p.stdin.close()
    assert_waiting(env)


@pytest.mark.parametrize("tasks", [[SHELL], []], ids=["background", "none"])
def test_no_waitd_socket_exits_zero(tmp_path, tasks):
    assert run_hook(tmp_path, stdin=bg_input(*tasks)).returncode == 0


def test_task_without_target_pings_legacy_waiting(env):
    from dispatcher.state import has_waiting
    assert run_hook(env, target=None, stdin="").returncode == 0
    assert has_waiting(env, "portfolio_eval", N)
