"""SIGTERM must stop the web process promptly even with live streams open.

The console always has long-lived connections (SSE /api/events, terminal
WebSocket).  uvicorn's graceful shutdown waits for open connections to
drain, and without a bound that wait is infinite — systemd then burns its
full 90 s stop timeout and SIGKILLs, turning every deploy into a ~90 s
outage (observed live on the box, 2026-07-30).

E2E: boot the real `python -m web` subprocess, hold an SSE stream open,
send SIGTERM, and require exit well inside systemd's stop timeout.
"""
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

EXIT_DEADLINE = 10  # generous for CI, far below systemd's 90 s


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, proc, deadline: float = 15.0) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(
                f"web process died during startup: {proc.stderr.read()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("web process never started listening")


@pytest.fixture
def web_process(tmp_path):
    (tmp_path / "targets.yaml").write_text(f"state_dir: {tmp_path}/state\n")
    (tmp_path / "state").mkdir()
    port = _free_port()
    env = dict(
        os.environ,
        AGENT_OPS_WEB_PORT=str(port),
        AGENT_OPS_STATE_DIR=str(tmp_path / "state"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "web", "--config",
         str(tmp_path / "targets.yaml")],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_listening(port, proc)
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _open_sse(port: int):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/events",
        headers={"Tailscale-User-Login": "jesdi@github"})
    return urllib.request.urlopen(req, timeout=10)


def test_sigterm_exits_promptly_with_open_sse_stream(web_process):
    proc, port = web_process
    stream = _open_sse(port)
    assert stream.status == 200

    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=EXIT_DEADLINE)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"web process still alive {EXIT_DEADLINE}s after SIGTERM with an "
            "open SSE stream — systemd would SIGKILL after 90 s (502 window)")
    finally:
        stream.close()
    # uvicorn re-raises SIGTERM after graceful shutdown; systemd's default
    # SuccessExitStatus counts death-by-SIGTERM as a clean stop.
    assert code in (0, -signal.SIGTERM)
