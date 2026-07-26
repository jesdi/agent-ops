"""Auth boundary: every request needs the Tailscale Whois header."""
import pytest
from fastapi.testclient import TestClient

from tests.webfakes import HEADERS, make_config


def _client(tmp_path):
    from web.app import create_app
    return TestClient(create_app(make_config(tmp_path), sources=None))


def test_missing_header_is_401(tmp_path):
    r = _client(tmp_path).get("/api/health")
    assert r.status_code == 401


def test_header_present_returns_operator_login(tmp_path):
    r = _client(tmp_path).get("/api/health", headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "operator": "jesdi@github"}


def test_ensure_loopback_accepts_localhost_names():
    from web.__main__ import ensure_loopback
    for host in ("127.0.0.1", "localhost", "::1"):
        ensure_loopback(host)  # must not raise


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.0.7", "192.168.1.5", ""])
def test_ensure_loopback_refuses_non_loopback(host):
    from web.__main__ import ensure_loopback
    with pytest.raises(SystemExit):
        ensure_loopback(host)
