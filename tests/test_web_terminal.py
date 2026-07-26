"""Terminal bridge against a stubbed tmux executable."""
import concurrent.futures
import json
import os
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


STUB = """#!/bin/sh
case "$*" in
  *new-session*) echo FAKE-TMUX-READY; exec cat ;;
  *kill-session*) exit 0 ;;
  *) exit 0 ;;
esac
"""

# Hard wall-clock deadline per ws.receive_bytes() call.  The sync TestClient
# blocks indefinitely inside the anyio portal — a bare iteration count does not
# bound wall time if the server never sends.  _recv_bounded() wraps the call in
# a thread and fails with a clear message if it exceeds the deadline.
_RECV_TIMEOUT = 5.0


def _recv_bounded(ws, timeout=_RECV_TIMEOUT):
    """Receive bytes from ws with a hard wall-clock deadline."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = executor.submit(ws.receive_bytes)
    try:
        result = fut.result(timeout=timeout)
        executor.shutdown(wait=False)
        return result
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        pytest.fail(
            f"ws.receive_bytes() did not return within {timeout}s — "
            "stub tmux likely failed to exec or produce output"
        )


def stub_tmux(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(STUB)
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


def rig(tmp_path):
    fake = FakeSources()
    return fake, TestClient(create_app(make_config(tmp_path), fake))


def test_ws_without_header_is_rejected(tmp_path):
    _, client = rig(tmp_path)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/task/7/terminal"):
            pass
    assert exc_info.value.code == 4401


def test_dead_session_sends_tail_and_closes(tmp_path):
    fake, client = rig(tmp_path)
    fake.pane_tails[7] = "last output"
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as ws:
        msg = ws.receive_json()
        assert msg == {"type": "dead", "tail": "last output"}
    assert 7 not in fake.attached


# forkpty() in a multi-threaded process emits a DeprecationWarning in
# Python 3.13.  The child branch does nothing but exec (or os._exit(127) on
# failure), which is the only safe pattern post-fork in a threaded process.
# The warning is narrowly suppressed here; it must not be silenced globally.
@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_live_session_echo_and_attach_markers(tmp_path, monkeypatch):
    stub_tmux(tmp_path, monkeypatch)
    fake, client = rig(tmp_path)
    fake.alive.add(7)
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as ws:
        buf = b""
        for _ in range(50):
            buf += _recv_bounded(ws)
            if b"FAKE-TMUX-READY" in buf:
                break
        else:
            pytest.fail(
                f"FAKE-TMUX-READY never arrived after 50 bounded reads; "
                f"got {buf!r}"
            )
        assert 7 in fake.attached
        ws.send_bytes(b"hello-terminal\n")
        buf = b""
        for _ in range(50):
            buf += _recv_bounded(ws)
            if b"hello-terminal" in buf:
                break
        else:
            pytest.fail(
                f"echo of hello-terminal never arrived after 50 bounded reads; "
                f"got {buf!r}"
            )
        # resize must not kill the bridge
        ws.send_text(json.dumps({"type": "resize",
                                 "cols": 120, "rows": 40}))
    # disconnect handler ran: marker cleared (poll briefly — the server
    # side finishes asynchronously after the client context exits)
    for _ in range(50):
        if 7 not in fake.attached:
            break
        time.sleep(0.05)
    assert 7 not in fake.attached
