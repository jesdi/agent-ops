import http.client
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher.waitd import handle_ping, serve


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, template, **ctx):
        self.sent.append((template, ctx))


def test_handle_ping():
    n = FakeNotifier()
    handle_ping(json.dumps({"issue": 42}).encode(), n)
    assert n.sent == [("waiting", {"issue": 42})]


def test_handle_ping_corrupt_body_drops():
    n = FakeNotifier()
    handle_ping(b"{nope", n)
    assert n.sent == []


def test_serve_end_to_end():
    # Use a short temp dir to avoid AF_UNIX path length limit (~104 bytes on macOS)
    tmpdir = tempfile.mkdtemp(prefix="w_")
    try:
        sock = Path(tmpdir) / "w.sock"
        n = FakeNotifier()
        t = threading.Thread(target=serve, args=(sock, n), daemon=True)
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
        assert n.sent == [("waiting", {"issue": 7})]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
