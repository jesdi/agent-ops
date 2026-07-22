import http.client
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher.state import has_waiting
from dispatcher.waitd import handle_ping, serve


def test_ping_marks_waiting(tmp_path):
    handle_ping(b'{"issue": 42}', tmp_path)
    assert has_waiting(tmp_path, 42)


def test_corrupt_ping_is_dropped(tmp_path):
    handle_ping(b'not json', tmp_path)
    handle_ping(b'{"issue": "x"}', tmp_path)
    assert list(tmp_path.glob("waiting-*")) == []


def test_serve_end_to_end():
    # Use a short temp dir to avoid AF_UNIX path length limit (~104 bytes on macOS)
    tmpdir = tempfile.mkdtemp(prefix="w_")
    try:
        sock = Path(tmpdir) / "w.sock"
        state_dir = Path(tmpdir) / "state"
        t = threading.Thread(target=serve, args=(sock, state_dir), daemon=True)
        t.start()
        for _ in range(100):  # wait for the socket to appear
            if sock.exists():
                break
            threading.Event().wait(0.05)

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
