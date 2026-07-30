import http.client
import json
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher.state import has_waiting
from dispatcher.waitd import handle_ping, serve, sock_path


def test_ping_marks_waiting(tmp_path):
    handle_ping(b'{"issue": 42}', tmp_path)
    assert has_waiting(tmp_path, 42)


def test_corrupt_ping_is_dropped(tmp_path):
    handle_ping(b'not json', tmp_path)
    handle_ping(b'{"issue": "x"}', tmp_path)
    assert list(tmp_path.glob("waiting-*")) == []


def test_sock_path_lives_in_its_own_dir(tmp_path):
    # The socket sits in a dedicated subdir so session containers can
    # bind-mount just that dir: a file bind would go stale when waitd
    # recreates the socket, and mounting the whole state dir would hand
    # op-token.env to every session.
    assert sock_path(tmp_path) == tmp_path / "wait" / "wait.sock"


def test_stop_hook_script_pings_waitd():
    # The real regression: sessions run the hook inside a container where
    # only the wait dir is mounted — the script must resolve the socket
    # via AGENT_OPS_STATE_DIR and reach waitd through it.
    tmpdir = tempfile.mkdtemp(prefix="w_")
    try:
        state_dir = Path(tmpdir)
        t = threading.Thread(target=serve,
                             args=(sock_path(state_dir), state_dir),
                             daemon=True)
        t.start()
        _await_listening(sock_path(state_dir))

        agent_dir = state_dir / "wt" / ".agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "task.json").write_text(json.dumps({"issue": 187}))
        script = Path(__file__).parent.parent / "hooks" / "stop-hook.sh"
        shutil.copy(script, agent_dir / "stop-hook.sh")
        r = subprocess.run(["bash", str(agent_dir / "stop-hook.sh")],
                           env={"PATH": "/usr/bin:/bin",
                                "AGENT_OPS_STATE_DIR": str(state_dir)},
                           timeout=30)
        assert r.returncode == 0
        assert has_waiting(state_dir, 187)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _await_listening(sock: Path):
    # Wait until the server actually accepts connections: the socket file
    # appears at bind(), before listen(), so existence alone can race
    # into ECONNREFUSED.
    for _ in range(100):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(sock))
            return
        except (ConnectionRefusedError, FileNotFoundError):
            threading.Event().wait(0.05)
        finally:
            probe.close()
    pytest.fail("waitd never started accepting connections")


def test_serve_end_to_end():
    # Use a short temp dir to avoid AF_UNIX path length limit (~104 bytes on macOS)
    tmpdir = tempfile.mkdtemp(prefix="w_")
    try:
        sock = Path(tmpdir) / "w.sock"
        state_dir = Path(tmpdir) / "state"
        t = threading.Thread(target=serve, args=(sock, state_dir), daemon=True)
        t.start()
        # Wait until the server actually accepts connections: the socket file
        # appears at bind(), before listen(), so existence alone can race
        # into ECONNREFUSED.
        for _ in range(100):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(sock))
                break
            except (ConnectionRefusedError, FileNotFoundError):
                threading.Event().wait(0.05)
            finally:
                probe.close()
        else:
            pytest.fail("waitd never started accepting connections")

        class UnixConn(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(str(sock))

        conn = UnixConn("localhost")
        body = json.dumps({"issue": 7})
        conn.request("POST", "/waiting", body=body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(body))})
        resp = conn.getresponse()
        assert resp.status == 200
        assert has_waiting(state_dir, 7)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
