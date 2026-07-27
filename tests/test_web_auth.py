"""Auth boundary: every request needs the Tailscale Whois header."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.webfakes import FakeSources, HEADERS, make_config, make_task


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


# -- cross-origin boundary -------------------------------------------------
# tailscale serve injects the Whois header on EVERY proxied request, including
# ones a third-party page makes from the operator's browser.  Unsafe methods
# and WebSocket handshakes therefore need an Origin check of their own.

def _intent_client(tmp_path):
    from web.app import create_app
    fake = FakeSources()
    fake.tasks_list = [make_task(issue=7)]
    fake.alive.add(7)
    return fake, TestClient(create_app(make_config(tmp_path), fake))


def test_cross_origin_bodyless_post_is_403_and_writes_no_intent(tmp_path):
    fake, client = _intent_client(tmp_path)
    r = client.post("/api/task/7/kill",
                    headers={**HEADERS, "Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert fake.intents == []


def test_same_origin_post_succeeds(tmp_path):
    fake, client = _intent_client(tmp_path)
    r = client.post("/api/task/7/kill",
                    headers={**HEADERS, "Origin": "http://testserver"})
    assert r.status_code == 202
    assert [i[0] for i in fake.intents] == ["kill"]


def test_origin_less_post_succeeds(tmp_path):
    """curl and the documented SSE verification send no Origin at all."""
    fake, client = _intent_client(tmp_path)
    r = client.post("/api/task/7/kill", headers=HEADERS)
    assert r.status_code == 202
    assert [i[0] for i in fake.intents] == ["kill"]


def test_cross_origin_get_still_succeeds(tmp_path):
    _, client = _intent_client(tmp_path)
    r = client.get("/api/health",
                   headers={**HEADERS, "Origin": "https://evil.example.com"})
    assert r.status_code == 200


def test_cross_origin_websocket_is_closed_4403(tmp_path):
    _, client = _intent_client(tmp_path)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
                "/api/task/7/terminal",
                headers={**HEADERS, "Origin": "https://evil.example.com"}):
            pass
    assert exc.value.code == 4403


def test_origin_host_match_is_case_insensitive_and_scheme_stripped(tmp_path):
    fake, client = _intent_client(tmp_path)
    r = client.post("/api/task/7/kill",
                    headers={**HEADERS, "Origin": "HTTPS://TestServer"})
    assert r.status_code == 202


def test_origin_with_different_port_is_rejected(tmp_path):
    fake, client = _intent_client(tmp_path)
    r = client.post("/api/task/7/kill",
                    headers={**HEADERS, "Origin": "http://testserver:9999"})
    assert r.status_code == 403
    assert fake.intents == []


def test_non_utf8_header_bytes_do_not_500(tmp_path):
    client = _client(tmp_path)
    r = client.get("/api/health",
                   headers=[(b"tailscale-user-login", b"jes\xffdi@github")])
    assert r.status_code != 500
