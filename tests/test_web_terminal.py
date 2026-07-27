"""Terminal bridge against a stubbed tmux executable."""
import asyncio
import concurrent.futures
import json
import os
import subprocess
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app
from web.terminal import AttachRegistry, run_terminal


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


def _wait_until(pred, timeout=2.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _drain_until_ready(ws):
    buf = b""
    for _ in range(50):
        buf += _recv_bounded(ws)
        if b"FAKE-TMUX-READY" in buf:
            return buf
    pytest.fail(f"FAKE-TMUX-READY never arrived; got {buf!r}")


@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_two_viewers_keep_marker_until_both_disconnect(tmp_path, monkeypatch):
    """Grouped tmux sessions exist so several viewers can attach at once; an
    SPA reload also overlaps old and new sockets. The dispatcher REFUSES to
    drive a task while attached-<N> exists, so one viewer leaving must not
    clear it out from under another."""
    stub_tmux(tmp_path, monkeypatch)
    fake, client = rig(tmp_path)
    fake.alive.add(7)
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as first:
        _drain_until_ready(first)
        assert 7 in fake.attached
        with client.websocket_connect("/api/task/7/terminal",
                                      headers=HEADERS) as second:
            _drain_until_ready(second)
            assert 7 in fake.attached
        # second viewer gone, first still typing: marker must survive
        time.sleep(0.3)
        assert 7 in fake.attached
    assert _wait_until(lambda: 7 not in fake.attached)


@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_attach_and_detach_are_logged_with_operator(tmp_path, monkeypatch):
    stub_tmux(tmp_path, monkeypatch)
    fake, client = rig(tmp_path)
    fake.alive.add(7)
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as ws:
        _drain_until_ready(ws)
        assert _wait_until(lambda: [e[0] for e in fake.appended]
                           == ["terminal-attach"])
        assert fake.appended[0][3] == "jesdi@github"
        assert fake.appended[0][2] == 7
    assert _wait_until(lambda: [e[0] for e in fake.appended]
                       == ["terminal-attach", "terminal-detach"])
    assert fake.appended[1][3] == "jesdi@github"


@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_malformed_resize_frames_do_not_drop_the_terminal(tmp_path,
                                                          monkeypatch):
    """A well-formed but non-object JSON body used to raise AttributeError
    out of d.get and tear the bridge down."""
    stub_tmux(tmp_path, monkeypatch)
    fake, client = rig(tmp_path)
    fake.alive.add(7)
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as ws:
        _drain_until_ready(ws)
        for frame in ('"3"', "3", "null", "[1,2]",
                      '{"type":"resize","cols":"wide","rows":40}',
                      '{"type":"resize","cols":0,"rows":0}', "not json"):
            ws.send_text(frame)
        ws.send_bytes(b"still-alive\n")
        buf = b""
        for _ in range(50):
            buf += _recv_bounded(ws)
            if b"still-alive" in buf:
                break
        else:
            pytest.fail(f"bridge died on a malformed frame; got {buf!r}")


TERM_STUB = """#!/bin/sh
case "$*" in
  *new-session*) echo "STUB-TERM=${TERM:-unset}"; exec cat ;;
  *kill-session*) exit 0 ;;
  *) exit 0 ;;
esac
"""


@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_child_gets_a_term_even_when_service_env_has_none(tmp_path,
                                                          monkeypatch):
    """Under systemd the web service has no TERM; real tmux then dies with
    'open terminal failed: terminal does not support clear'. The bridge must
    hand the pty child a usable TERM regardless of the parent env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(TERM_STUB)
    tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("TERM", raising=False)
    fake, client = rig(tmp_path)
    fake.alive.add(7)
    with client.websocket_connect("/api/task/7/terminal",
                                  headers=HEADERS) as ws:
        buf = b""
        for _ in range(50):
            buf += _recv_bounded(ws)
            if b"STUB-TERM=" in buf:
                break
        else:
            pytest.fail(f"stub never reported TERM; got {buf!r}")
    assert b"STUB-TERM=xterm-256color" in buf


class FakeWS:
    """Minimal WebSocket stand-in so run_terminal can be driven directly,
    outside the TestClient portal (which cancels the handler at teardown and
    would mask what the cleanup path does)."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.headers = {"tailscale-user-login": "jesdi@github"}

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(text)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True

    async def receive(self):
        return {"type": "websocket.disconnect"}


@pytest.mark.filterwarnings(
    "ignore:This process.*is multi-threaded.*:DeprecationWarning"
)
def test_cleanup_survives_a_reap_that_raises(tmp_path, monkeypatch):
    """The trailing reap is the one statement that runs during loop teardown;
    unguarded it masks any in-flight exception and escapes run_terminal."""
    stub_tmux(tmp_path, monkeypatch)

    def boom(pid):
        raise RuntimeError("reap exploded")

    monkeypatch.setattr("web.terminal._reap", boom)
    fake = FakeSources()
    fake.alive.add(7)
    viewers = AttachRegistry(fake)
    ws = FakeWS()
    # must return normally, not raise
    asyncio.run(run_terminal(ws, 7, fake, viewers, actor="jesdi@github"))
    assert 7 not in fake.attached
    assert [e[0] for e in fake.appended] == ["terminal-attach",
                                             "terminal-detach"]


def test_attach_registry_refcounts_the_marker():
    fake = FakeSources()
    viewers = AttachRegistry(fake)
    viewers.attach(7)
    viewers.attach(7)
    assert fake.attached == {7}
    viewers.detach(7)
    assert fake.attached == {7}   # second viewer still there
    viewers.detach(7)
    assert fake.attached == set()
    assert viewers.viewers(7) == 0
    viewers.detach(7)             # unbalanced detach must not explode
    assert fake.attached == set()


def test_kill_session_is_time_bounded_and_survives_a_hung_tmux():
    """asyncio.wait_for bounds the await, not the subprocess: without a
    timeout= a hung tmux parks a default-executor thread forever."""
    from web import terminal

    calls = {}

    def fake_run(cmd, **kw):
        calls.update(cmd=cmd, kw=kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    original = subprocess.run
    subprocess.run = fake_run
    try:
        terminal._kill_session("view-abc")  # must not raise
    finally:
        subprocess.run = original
    assert calls["cmd"] == ["tmux", "kill-session", "-t", "view-abc"]
    assert calls["kw"]["timeout"] == 10
