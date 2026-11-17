"""The usage socket answers GET /api/usage (ticket 01) as well as /usage, same body."""
from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, make_config
from web.app import create_app


def test_socket_serves_api_usage_like_usage(tmp_path):
    app = create_app(make_config(tmp_path), FakeSources())
    sock = TestClient(app.state.usage_app)
    r = sock.get("/api/usage")
    assert r.status_code == 200
    assert r.json().keys() == sock.get("/usage").json().keys()
    assert sock.get("/api/board").status_code == 404
