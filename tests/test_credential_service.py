"""Production credential wiring and renewable command consumers (ticket 07)."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dispatcher import containers, credential_service as service
from dispatcher.execution_credentials import CredentialDenied, CredentialError


def config(projects=()):
    return SimpleNamespace(targets=[SimpleNamespace(repo="me/old", name="old")],
                           projects=list(projects))


def test_startup_defaults_to_service_and_requires_explicit_legacy_mode(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_LEGACY_CREDENTIALS", raising=False)
    project = SimpleNamespace(key="analytics", repositories=[SimpleNamespace(repo="me/analytics")])
    client = service.configured_credentials(config([project]))
    assert isinstance(client, service.CredentialService)
    assert client.repositories == {"me/old": "old", "me/analytics": "analytics"}
    monkeypatch.setenv("AGENT_OPS_CREDENTIAL_SOCKET", "/trusted/service.sock")
    assert service.configured_credentials(config()).path == "/trusted/service.sock"
    monkeypatch.setenv("AGENT_OPS_LEGACY_CREDENTIALS", "1")
    assert service.configured_credentials(config()) is None
    with pytest.raises(CredentialDenied, match="legacy"):
        service.configured_credentials(config([project]))


def test_service_registers_scope_and_requests_tokens_without_secret_transport(monkeypatch):
    connection = Mock()
    response = connection.getresponse.return_value
    response.status = 200
    response.read.return_value = json.dumps(dict(token="scoped", repository_id=101,
        permissions={"contents": "read"}, expires_at=1234)).encode()
    monkeypatch.setattr(service, "_Connection", lambda *a, **kw: connection)
    client = service.CredentialService("/trusted/service.sock", {"me/analytics": "analytics"})
    client.register_execution("triage-analytics", "me/analytics", "triage")
    sent = json.loads(connection.request.call_args.args[2])
    assert sent == dict(execution_id="triage-analytics", project="analytics",
                        repositories=["me/analytics"], purpose="triage")
    assert client.acquire("triage-analytics", "me/analytics", "triage").token == "scoped"
    assert client.refresh("triage-analytics", "me/analytics", "triage").repository_id == "101"
    assert connection.request.call_args.args[1] == "/v1/refresh"
    with pytest.raises(CredentialDenied):
        client.register_execution("triage-analytics", "me/billing", "triage")
    with pytest.raises(CredentialDenied):
        client.register_execution("triage-analytics", "me/analytics", "board")
    response.status = 403
    with pytest.raises(CredentialError, match="HTTP 403"):
        client.acquire("triage-analytics", "me/billing", "triage")
    assert connection.close.called


def test_unix_connection_uses_only_configured_socket(monkeypatch):
    sock = Mock()
    monkeypatch.setattr(service.socket, "socket", lambda *args: sock)
    connection = service._Connection("/trusted/service.sock", timeout=30)
    connection.connect()
    sock.connect.assert_called_once_with("/trusted/service.sock")
    sock.settimeout.assert_called_once_with(30)


def test_running_command_consumers_read_replacements_and_refuse_missing_token(tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(herdr, "tab_absent", lambda name: False)
    client = Mock()
    client.clock.return_value = 600  # A has expired by the next command.
    client.refresh.return_value = SimpleNamespace(token="B", expires_at=1600)
    sessions = Sessions(credentials=client)
    sessions._deliver_token("task-1", "me/analytics",
                            SimpleNamespace(token="A", expires_at=100))
    # Stand in for the container's /usr/bin/gh and /usr/bin/git at the
    # executable boundary; run the actual consumer scripts in subprocesses.
    commands = Path(containers.__file__).parent / "credential_commands"
    fake_gh = tmp_path / "real-gh"
    fake_gh.write_text('#!/bin/sh\nif [ "$1" = auth ]; then\n  printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"\nelse\n  printf "%s" "$GH_TOKEN"\nfi\n')
    fake_gh.chmod(0o755)
    gh = tmp_path / "gh"
    gh.write_text((commands / "gh").read_text().replace("/usr/bin/gh", str(fake_gh)))
    gh.chmod(0o755)
    git = tmp_path / "git"
    git.write_text((commands / "git").read_text().replace("/opt/agent-ops-auth/gh", str(gh)))
    git.chmod(0o755)
    token = tmp_path / "tokens" / "task-1" / "gh-token"
    env = {**os.environ, "GH_TOKEN": "stale", "GH_TOKEN_FILE": str(token)}
    for value in ["A", "B"]:
        if value == "B":
            sessions.renew_tokens()
            client.refresh.assert_called_once_with("task-1", "me/analytics", "task")
        assert subprocess.check_output([str(gh), "api", "user"], env=env, text=True) == value
        result = subprocess.run([str(git), "credential", "fill"], env=env,
            input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True)
        # The helper is gh, which must receive the refreshed token even
        # when Git launches it from a process that inherited stale GH_TOKEN.
        assert result.returncode == 0, result.stderr
        assert f"password={value}" in result.stdout
    token.unlink()
    assert subprocess.run([str(gh), "api", "user"], env=env, capture_output=True).returncode != 0


def test_scoped_launch_installs_consumers_and_renewable_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(containers, "_runtime_args", lambda runtime, name="": [])
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    cmd = containers.session_cmd("task-1", str(tmp_path), "2g", "2", "claude-fable-5", "P", scoped_token=True)
    assert "GH_TOKEN_FILE=/run/agent-ops/gh-token" in cmd
    assert f"{tmp_path}/tokens/task-1:/run/agent-ops:ro" in cmd
    assert "/credential_commands:/opt/agent-ops-auth:ro" in cmd
    assert "PATH=/opt/agent-ops-auth:/usr/local/bin:/usr/bin:/bin" in cmd
    assert "/root/.config/gh" not in cmd


def test_main_injects_one_service_into_all_production_dependencies(tmp_path, monkeypatch):
    from dispatcher import main
    from tests.test_main import cfg
    c = cfg(tmp_path)
    monkeypatch.setattr(main, "_load", lambda path: (c, c))
    monkeypatch.setattr(main, "referenced_providers", lambda cfg: set())
    monkeypatch.setattr("sys.argv", ["agent-ops-dispatcher", "--digest"])
    seen = []
    monkeypatch.setattr(main, "send_digest", lambda cfg, deps: seen.append(deps))
    main.main()
    deps = seen[0]
    assert isinstance(deps.credentials, service.CredentialService)
    assert deps.github.credentials is deps.credentials
    assert deps.sessions.credentials is deps.credentials


def test_launch_registers_before_refresh_and_never_runs_after_registration_failure(tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions, session_name
    from tests.test_containers import make_worktree
    wt, clone = make_worktree(tmp_path)
    client = service.CredentialService("/trusted/service.sock", {"me/analytics": "analytics"})
    operations = []

    def request(operation, body):
        operations.append((operation, body))
        if operation == "executions/project-env":
            return {"env": {}}
        return dict(token="A", repository_id=101, permissions={}, expires_at=1234)

    monkeypatch.setattr(client, "_request", request)
    tab = Mock()
    tab.run.return_value = True
    ensure = Mock(return_value=tab)
    monkeypatch.setattr(herdr.Tab, "ensure", ensure)
    sessions = Sessions(clones={"me/analytics": clone}, credentials=client)
    sessions.resume("analytics", 7, wt, "go", "anthropic/claude-opus-5-5",
                    session_id="session", repo="me/analytics")
    assert [op for op, body in operations] == [
        "executions/register", "executions/project-env", "refresh"]
    assert operations[0][1]["execution_id"] == session_name("analytics", 7, "me/analytics")
    ensure.reset_mock()
    client.repositories.clear()
    with pytest.raises(CredentialDenied):
        sessions.resume("analytics", 7, wt, "go", "anthropic/claude-opus-5-5",
                        session_id="session", repo="me/analytics")
    ensure.assert_not_called()


def test_repeated_triage_launch_has_a_token_for_the_whole_session(tmp_path):
    from dispatcher import triage
    from dispatcher.execution_credentials import ExecutionCredentials, ExecutionScope, Installation
    from tests.test_triage import BLOB, _sweep_cfg

    now = [0]
    app = Mock()
    app.installation_for.return_value = Installation(1, False, frozenset({101}))
    app.unmet_grants.return_value = []
    app.create_token.side_effect = lambda *args: (f"token-{now[0]}", now[0] + 3600)
    credentials = ExecutionCredentials(app, {
        "triage-o-a": ExecutionScope("a", "triage", {101: "o/a"}),
    }, clock=lambda: now[0])
    cfg = _sweep_cfg(tmp_path)
    launched = []

    def run(cmd, **kwargs):
        token = kwargs["env"]["GH_TOKEN"]
        launched.append(token)
        # A request late in the permitted session must still authenticate.
        issued_at = int(token.removeprefix("token-"))
        assert issued_at + 3600 > now[0] + triage.SESSION_TIMEOUT_SECONDS
        (tmp_path / "triage" / "o-a-2026-10-18.json").write_text('{"issues": []}')
        return subprocess.CompletedProcess(cmd, 0, "", "")

    for instant in (0, 3500):
        now[0] = instant
        triage._run_session(cfg, "o/a", BLOB, "2026-10-18", cfg.models.triage[0],
                            run=run, credentials=credentials)
    assert launched == ["token-0", "token-3500"]


@pytest.mark.parametrize("lifetime", [0, 1200, 1260, float("nan")])
def test_triage_refuses_insufficient_refreshed_lifetime(tmp_path, lifetime):
    from dispatcher import triage
    from tests.test_triage import BLOB, _sweep_cfg

    cfg = _sweep_cfg(tmp_path)
    client = Mock()
    client.clock.return_value = 3500
    client.refresh.return_value = SimpleNamespace(token="short", expires_at=3500 + lifetime)
    run = Mock()
    with pytest.raises(CredentialError, match="lifetime"):
        triage._run_session(cfg, "o/a", BLOB, "2026-10-18", cfg.models.triage[0],
                            run=run, credentials=client)
    run.assert_not_called()


@pytest.mark.parametrize("legacy", ["0", "1"])
def test_triage_runner_receives_credential_configuration(tmp_path, monkeypatch, legacy):
    from unittest.mock import patch
    from dispatcher import herdr, triage
    from tests.test_triage import FakeDeps, _sweep_cfg

    cfg = _sweep_cfg(tmp_path)
    monkeypatch.setattr(triage, "running", lambda: False)
    monkeypatch.setenv("AGENT_OPS_CREDENTIAL_SOCKET", "/trusted/custom.sock")
    monkeypatch.setenv("AGENT_OPS_LEGACY_CREDENTIALS", legacy)
    ensure = Mock(return_value=Mock())
    monkeypatch.setattr(herdr.Tab, "ensure", ensure)
    triage.enqueue(tmp_path)
    triage.tick(cfg, FakeDeps(), "targets.yaml")
    # The runner inherits only the explicitly forwarded environment here,
    # as when the independent herdr server lacks dispatcher configuration.
    with patch.dict(os.environ, ensure.call_args.kwargs["env"], clear=True):
        client = service.configured_credentials(cfg)
    if legacy == "1":
        assert client is None
    else:
        assert client.path == "/trusted/custom.sock"
