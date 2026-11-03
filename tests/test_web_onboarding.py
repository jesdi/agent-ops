"""Onboarding through the authenticated HTTP interface and the real intent queue."""
import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

REQUEST = {"repos": ["jesdi/analytics"], "board": {"owner": "jesdi", "number": 4},
           "commands": [{"setup_cmd": "uv sync", "gate_cmd": "make gate", "verify_cmd": ""}]}


@pytest.fixture
def console(tmp_path, monkeypatch):
    calls = []
    def execute(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr("web.sources.subprocess.run", execute)
    cfg = make_config(tmp_path / "state")
    sources = Sources(cfg.state_dir, None, None, clock=lambda: 1000.0)
    return TestClient(create_app(cfg, sources)), calls


def test_started_onboarding_is_visible_before_the_unit_runs(console):
    client, calls = console
    response = client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    assert response.status_code == 202
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["status"] == "queued"
    assert view["request"] == REQUEST
    assert view["pending"][0]["action"] == "add"
    assert view["pending"][0]["actor"] == HEADERS["Tailscale-User-Login"]
    assert client.get("/api/onboarding", headers=HEADERS).json()["onboardings"] == [view]
    assert calls == [("systemctl", "--user", "start", "--no-block", "agent-ops-onboard.service")]


@pytest.mark.parametrize("body", [
    {**REQUEST, "repos": []},
    {**REQUEST, "repos": ["/tmp/local"]},
    {**REQUEST, "commands": []},
    {**REQUEST, "commands": [{"gate_cmd": "  "}]},
    {**REQUEST, "board": {"owner": "../escape", "number": 4}},
    {**REQUEST, "board": {"owner": "jesdi", "number": 0}},
    {**REQUEST, "repos": ["jesdi/analytics", "https://github.com/JESDI/analytics.git"],
     "commands": REQUEST["commands"] * 2},
])
def test_invalid_request_never_becomes_pending(console, body):
    client, calls = console
    assert client.post("/api/onboarding/analytics", headers=HEADERS, json=body).status_code == 422
    assert client.get("/api/onboarding", headers=HEADERS).json() == {"onboardings": []}
    assert calls == []


@pytest.mark.parametrize("project", ["%2E%2E", "bad name", "UPPER"])
def test_project_paths_are_slugs(console, project):
    client, _ = console
    assert client.post(f"/api/onboarding/{project}", headers=HEADERS, json=REQUEST).status_code in (404, 422)
    assert client.get(f"/api/onboarding/{project}", headers=HEADERS).status_code in (404, 422)


def test_onboarding_requires_identity_and_first_party_writes(console):
    client, calls = console
    assert client.get("/api/onboarding").status_code == 401
    assert client.post("/api/onboarding/analytics", json=REQUEST).status_code == 401
    assert client.post("/api/onboarding/analytics", headers={**HEADERS, "Origin": "https://evil.test"}, json=REQUEST).status_code == 403
    assert calls == []


def run_unit(tmp_path, monkeypatch):
    from pathlib import Path
    from dispatcher import onboard
    from tests.test_onboard import LIVE, FakeSh
    monkeypatch.setenv("HOME", str(Path.home()))
    config = tmp_path / "targets.yaml"
    config.write_text(LIVE)
    onboard.run(config, sh=FakeSh())


def test_cli_started_record_is_reviewed_and_confirmed_in_console(console, tmp_path, monkeypatch):
    from dispatcher import onboard
    client, _ = console
    onboard.write_intent(tmp_path / "state", "add", "analytics", REQUEST, "agent", 1)
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["status"] == "open"
    assert view["awaiting"] == "confirm"
    assert view["proposal"]["repositories"][0]["repo"] == "jesdi/analytics"
    assert view["proposal"]["repositories"][0]["gate_cmd"] == "make gate"
    assert client.post("/api/onboarding/analytics/confirm", headers=HEADERS,
                       json={"revision": "stale"}).status_code == 409
    response = client.post("/api/onboarding/analytics/confirm", headers=HEADERS,
                           json={"revision": view["revision"]})
    assert response.status_code == 202
    assert client.get("/api/onboarding/analytics", headers=HEADERS).json()["pending"][0]["action"] == "confirm"
    run_unit(tmp_path, monkeypatch)
    confirmed = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert confirmed["confirmed_revision"] == view["revision"]
    assert confirmed["awaiting"] == ""
    assert confirmed["pending"] == []


def test_resume_cannot_replay_an_old_request_over_a_concurrent_edit(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    run_unit(tmp_path, monkeypatch)
    assert client.post("/api/onboarding/analytics/resume", headers=HEADERS).status_code == 202
    changed = {**REQUEST, "commands": [{"setup_cmd": "", "gate_cmd": "make new-gate", "verify_cmd": ""}]}
    client.post("/api/onboarding/analytics", headers=HEADERS, json=changed)
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["request"] == changed
    assert view["proposal"]["repositories"][0]["gate_cmd"] == "make new-gate"
    assert view["pending"] == []


def test_confirm_is_rechecked_when_the_unit_consumes_it(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    run_unit(tmp_path, monkeypatch)
    old = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "commands": [{"gate_cmd": "make changed"}]})
    assert client.post("/api/onboarding/analytics/confirm", headers=HEADERS,
                       json={"revision": old["revision"]}).status_code == 202
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["revision"] != old["revision"]
    assert view["awaiting"] == "confirm"
    assert next(s for s in view["steps"] if s["name"] == "config-pr")["state"] == "pending"


def test_invalid_cli_request_is_readable_as_a_blocked_onboarding(console, tmp_path, monkeypatch):
    from dispatcher import onboard
    client, _ = console
    onboard.write_intent(tmp_path / "state", "add", "analytics",
                         {**REQUEST, "repos": ["/tmp/local"]}, "agent", 1)
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["request"]["repos"] == ["/tmp/local"]
    assert view["steps"][0]["state"] == "blocked"


def test_malformed_pending_files_are_not_deleted_or_shown(console, tmp_path):
    client, _ = console
    root = tmp_path / "state" / "onboarding-intents"
    root.mkdir(parents=True)
    for name, body in {"a.json": "not json", "b.json": "[]", "c.json": '{"action":"add"}',
                       "d.json": '{"action":"resume","project":"../escape","payload":{}}'}.items():
        (root / name).write_text(body)
    assert client.get("/api/onboarding", headers=HEADERS).json() == {"onboardings": []}
    assert len(list(root.iterdir())) == 4


def test_a_failed_service_kick_keeps_the_durable_intent(console, monkeypatch):
    client, _ = console
    def fail(*args, **kwargs):
        raise OSError("systemctl unavailable")
    monkeypatch.setattr("web.sources.subprocess.run", fail)
    assert client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST).status_code == 202
    assert client.get("/api/onboarding/analytics", headers=HEADERS).json()["status"] == "queued"


def test_same_millisecond_writes_do_not_overwrite_pending_actions(console):
    client, _ = console
    first = client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST).json()
    second = client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST).json()
    assert first["intent"] != second["intent"]
    assert len(client.get("/api/onboarding/analytics", headers=HEADERS).json()["pending"]) == 2


def test_completed_record_stays_visible_but_cannot_confirm_or_resume(console, tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_onboard import LIVE, FakeGet, LISTED, deployed
    client, _ = console
    monkeypatch.setenv("HOME", str(Path.home()))
    monkeypatch.setenv("GH_INFRA_TOKEN", "synthetic-test-token")
    config = tmp_path / "targets.yaml"
    config.write_text(LIVE)
    deployed(tmp_path, config, FakeGet(LISTED))
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["status"] == "complete"
    assert view["proposal"]["paused"] is True
    assert view["pr_url"]
    assert client.get("/api/onboarding", headers=HEADERS).json()["onboardings"] == [view]
    assert client.post("/api/onboarding/analytics/resume", headers=HEADERS).status_code == 409
    assert client.post("/api/onboarding/analytics/confirm", headers=HEADERS,
                       json={"revision": view["revision"]}).status_code == 409


def test_missing_onboarding_cannot_be_resumed_or_confirmed(console):
    client, _ = console
    assert client.get("/api/onboarding/missing", headers=HEADERS).status_code == 404
    assert client.post("/api/onboarding/missing/resume", headers=HEADERS).status_code == 404
    assert client.post("/api/onboarding/missing/confirm", headers=HEADERS,
                       json={"revision": "unknown"}).status_code == 404
