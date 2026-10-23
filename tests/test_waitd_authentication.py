"""Socket authentication cannot be bypassed by omitting repository identity."""
import http.client
import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher import state, waitd
from tests.webfakes import make_task


@pytest.fixture
def socket_ping(monkeypatch):
    # Model a foreign container: its kernel peer is not the legacy task's peer.
    monkeypatch.setattr(waitd, "_legacy_peer_matches", lambda *args: False)
    with tempfile.TemporaryDirectory(prefix="wa_", dir="/tmp") as temporary:
        root = Path(temporary)
        server = waitd._Server(root / "w.sock", root)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        class UnixConnection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(str(root / "w.sock"))

        def ping(payload):
            connection = UnixConnection("localhost")
            connection.request("POST", "/waiting", json.dumps(payload))
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 200

        try:
            yield root, ping
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


@pytest.mark.parametrize("extra", [
    {"session_id": "forged"},
    {"background_tasks": [{"id": "foreign", "type": "shell"}]},
    {"runtime": "codex", "session_id": "forged"},
])
def test_encoded_target_cannot_overwrite_qualified_task(socket_ping, extra):
    root, ping = socket_ping
    repo, target, issue = "example/shop-backend", "shop", 12
    state.save(root, make_task(issue=issue, target=target, repo=repo))
    token = state.issue_execution_token(root, target, issue, repo)
    state.write_session(root, target, issue, state.SessionRecord("original", "implement"),
                        repo=repo)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    ping({"target": "example~shop-backend~shop", "issue": issue, **extra})
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    # A genuine execution remains able to use its token on this same transport.
    ping({"target": target, "repo": repo, "issue": issue, "token": token,
          "session_id": "own"})
    assert state.read_session(root, target, issue, repo=repo).session_id == "own"
    assert state.has_waiting(root, target, issue, repo=repo)


@pytest.mark.parametrize("target", ["shop", ""])
def test_unmapped_socket_ping_still_requires_peer_authentication(socket_ping, target):
    root, ping = socket_ping
    ping({"target": target, "issue": 12, "session_id": "forged"})
    assert not [p for p in root.rglob("*") if p.is_file() and p.name != "migration.lock"]


@pytest.mark.parametrize("payload", [
    [], {"target": []}, {"target": "shop/../other"},
    {"target": "example~backend~shop"}, {"target": "shop\x00"},
    {"target": "shop", "repo": ["example/backend"]},
    {"target": "shop", "repo": "example~backend"},
    {"target": "", "repo": "example/backend"},
])
def test_invalid_identity_is_rejected_before_state_lookup(tmp_path, monkeypatch, payload):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid identity reached state lookup")

    monkeypatch.setattr(waitd, "session_owner", unexpected)
    monkeypatch.setattr(waitd, "_load_session_task", unexpected)
    body = {"issue": 12, **payload} if isinstance(payload, dict) else payload
    state_dir = tmp_path / "state"
    waitd.handle_ping(json.dumps(body).encode(), state_dir)
    assert not state_dir.exists()


def test_unmapped_legacy_sender_is_checked_before_writing(tmp_path):
    calls = []

    def sender(target, issue):
        calls.append((target, issue))
        return True

    waitd.handle_ping(b'{"target":"shop","issue":12}', tmp_path, legacy_sender=sender)
    assert calls == [("shop", 12)]
    assert state.has_waiting(tmp_path, "shop", 12)
