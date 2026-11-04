"""The real `_http` transport of dispatcher.github_app, against a local server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dispatcher import github_app


class Handler(BaseHTTPRequestHandler):
    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        echo = json.loads(self.rfile.read(n)) if n else {}
        status = {"/ok": 200, "/missing": 404}.get(self.path, 200)
        raw = b"not json" if self.path == "/garbage" else json.dumps({"echo": echo}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


@pytest.fixture
def base():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_http_returns_status_and_json(base):
    assert github_app._http("POST", base + "/ok", {}, {"a": 1}) == (200, {"echo": {"a": 1}})


def test_http_returns_an_error_status_instead_of_raising(base):
    assert github_app._http("GET", base + "/missing", {})[0] == 404


def test_http_tolerates_a_non_json_body(base):
    assert github_app._http("GET", base + "/garbage", {}) == (200, {})


def test_http_reports_an_unreachable_host_as_a_timeout():
    with pytest.raises(TimeoutError):
        github_app._http("GET", "http://127.0.0.1:1/", {})
