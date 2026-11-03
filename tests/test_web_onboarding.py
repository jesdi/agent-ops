"""Onboarding through the authenticated HTTP interface and the real intent queue."""
import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

REQUEST = {"inspect": False, "repos": ["jesdi/analytics"], "board": {"owner": "jesdi", "number": 4},
           "commands": [{"setup_cmd": "uv sync", "gate_cmd": "make gate", "verify_cmd": "", "services": None}]}


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
    changed = {**REQUEST, "commands": [{"setup_cmd": "", "gate_cmd": "make new-gate", "verify_cmd": "", "services": None}]}
    current = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **changed, "expected_request_revision": current["request_revision"]})
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
        **REQUEST, "commands": [{"gate_cmd": "make changed"}],
        "expected_request_revision": old["request_revision"]})
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
    assert view["edit_mode"] == "none"
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


def test_cli_project_name_remains_addressable_in_console(console, tmp_path):
    from dispatcher import onboard
    client, _ = console
    project = "a" * 101
    onboard.write_intent(tmp_path / "state", "add", project, REQUEST, "agent", 1)
    listing = client.get("/api/onboarding", headers=HEADERS).json()["onboardings"]
    assert listing[0]["project"] == project
    detail = client.get(f"/api/onboarding/{project}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json() == listing[0]


def test_edit_rejects_a_stale_snapshot_without_losing_the_newer_request(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    run_unit(tmp_path, monkeypatch)
    before = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    changed = {**REQUEST, "commands": [{"gate_cmd": "make newer", "setup_cmd": "", "verify_cmd": "", "services": None}]}
    saved = client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **changed, "expected_request_revision": before["request_revision"]})
    assert saved.status_code == 202
    run_unit(tmp_path, monkeypatch)
    stale = client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "board": {"owner": "jesdi", "number": 5},
        "expected_request_revision": before["request_revision"]})
    assert stale.status_code == 409
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["request"] == changed
    assert view["request_revision"] == saved.json()["request_revision"]


def test_queued_edits_recheck_the_request_revision_in_the_engine(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    run_unit(tmp_path, monkeypatch)
    before = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    first = {**REQUEST, "commands": [{"gate_cmd": "make newer", "setup_cmd": "", "verify_cmd": "", "services": None}]}
    second = {**REQUEST, "board": {"owner": "jesdi", "number": 5}}
    for request in (first, second):
        response = client.post("/api/onboarding/analytics", headers=HEADERS, json={
            **request, "expected_request_revision": before["request_revision"]})
        assert response.status_code == 202
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["request"] == first
    assert "changed" in view["notice"]
    assert view["pending"] == []


def test_web_start_never_replaces_an_existing_different_request(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    run_unit(tmp_path, monkeypatch)
    assert client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "board": {"owner": "jesdi", "number": 5}}).status_code == 409
    assert client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST).status_code == 202


def test_engine_exposes_and_enforces_edit_permissions(console, tmp_path, monkeypatch):
    client, _ = console
    client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    assert client.get("/api/onboarding/analytics", headers=HEADERS).json()["edit_mode"] == "none"
    run_unit(tmp_path, monkeypatch)
    before = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert before["edit_mode"] == "repositories"
    client.post("/api/onboarding/analytics/confirm", headers=HEADERS, json={"revision": before["revision"]})
    run_unit(tmp_path, monkeypatch)
    confirmed = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert confirmed["edit_mode"] == "details"
    changed = {**REQUEST, "repos": ["jesdi/another"],
               "expected_request_revision": confirmed["request_revision"]}
    assert client.post("/api/onboarding/analytics", headers=HEADERS, json=changed).status_code == 202
    run_unit(tmp_path, monkeypatch)
    refused = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert refused["request"] == REQUEST
    assert "repositories can't change" in refused["notice"]


def test_two_queued_web_starts_keep_the_first_request(console, tmp_path, monkeypatch):
    client, _ = console
    first = client.post("/api/onboarding/analytics", headers=HEADERS, json=REQUEST)
    second = client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "board": {"owner": "jesdi", "number": 5}})
    assert first.status_code == second.status_code == 202
    run_unit(tmp_path, monkeypatch)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["request"] == REQUEST
    assert view["request_revision"] == first.json()["request_revision"]
    assert view["request_revision"] != second.json()["request_revision"]
    assert "changed" in view["notice"]


def test_edit_of_a_missing_record_is_refused_without_creating_it(console):
    client, _ = console
    response = client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "expected_request_revision": "a-removed-record"})
    assert response.status_code == 409
    assert client.get("/api/onboarding", headers=HEADERS).json() == {"onboardings": []}


def test_merged_onboarding_exposes_no_edits_and_refuses_a_queued_change(console, tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_onboard import LIVE, merged
    client, _ = console
    monkeypatch.setenv("HOME", str(Path.home()))
    monkeypatch.setenv("GH_INFRA_TOKEN", "synthetic-test-token")
    config = tmp_path / "targets.yaml"
    config.write_text(LIVE)
    merged(tmp_path, config)
    view = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert view["status"] == "open"
    assert view["edit_mode"] == "none"
    response = client.post("/api/onboarding/analytics", headers=HEADERS, json={
        **REQUEST, "board": {"owner": "jesdi", "number": 5},
        "expected_request_revision": view["request_revision"]})
    assert response.status_code == 202
    run_unit(tmp_path, monkeypatch)
    refused = client.get("/api/onboarding/analytics", headers=HEADERS).json()
    assert refused["request"] == view["request"]
    assert "merged into infra" in refused["notice"]


def test_start_with_only_repositories_defaults_to_inspection(console, tmp_path, monkeypatch):
    client, _ = console
    response = client.post('/api/onboarding/analytics', headers=HEADERS,
                           json={'repos': ['jesdi/analytics']})
    assert response.status_code == 202
    run_unit(tmp_path, monkeypatch)
    view = client.get('/api/onboarding/analytics', headers=HEADERS).json()
    assert view['request']['inspect'] is True
    assert view['proposal']['board'] is None
    assert not view['can_confirm']
    assert view['inspection']['repositories'][0]['state'] == 'waiting'
    assert client.post('/api/onboarding/analytics/confirm', headers=HEADERS,
                       json={'revision': view['revision']}).status_code == 409
    assert client.post('/api/onboarding/analytics/skip-inspection', headers=HEADERS,
                       json={'revision': 'stale'}).status_code == 409
    assert client.post('/api/onboarding/analytics/skip-inspection', headers=HEADERS,
                       json={'revision': view['revision']}).status_code == 202
    run_unit(tmp_path, monkeypatch)
    view = client.get('/api/onboarding/analytics', headers=HEADERS).json()
    assert view['inspection']['repositories'][0]['state'] == 'skipped'
    assert view['awaiting'] == 'confirm'
    assert not view['can_confirm']
