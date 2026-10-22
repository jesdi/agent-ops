"""Acceptance tests for ticket 07 (project-onboarding): scoped App credentials.

Spec: specs/project-onboarding/spec.md requirement R7/R8, scenarios S12, S15.
Seam: CREDENTIAL -- ExecutionCredentials.acquire/refresh(execution_id,
repository_id, purpose), over a faked GitHub App edge and a fake clock.

Interface these tests define (dispatcher/execution_credentials.py):
- Caller identity is the execution id, looked up in the recorded
  `executions` registry (ExecutionScope: project, kind, {repo id: full name}).
- Refusals are `CredentialDenied`; GitHub-side causes are the distinct
  `MissingInstallation`, `MissingRepositoryAccess`,
  `OrganizationApprovalPending` and `TokenGenerationFailed`.
- Board/control credentials come only from `TRUSTED`, resolved from 1Password
  references; for purpose "board" the second argument is the registered
  board key "owner/number".
"""
import subprocess
import threading
from pathlib import Path

import pytest

from dispatcher.execution_credentials import (
    TRUSTED, CredentialDenied, ExecutionCredentials, ExecutionScope,
    Installation, MissingInstallation, MissingRepositoryAccess,
    OrganizationApprovalPending, TokenGenerationFailed, UnresolvedGrant)

ANALYTICS, BILLING = 101, 202
APP_KEY = "-----BEGIN PRIVATE KEY-----SECRET-APP-KEY"
BOARD_REF, CONTROL_REF = "op://vault/account-board", "op://vault/infra-control"
VALUES = {BOARD_REF: "board-secret-value", CONTROL_REF: "control-secret-value"}
LIFETIME = 3600


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class App:
    """The GitHub App edge. Records every token request; holds the key."""

    def __init__(self, clock, installation=None):
        self.private_key = APP_KEY
        self.clock = clock
        self.installation = installation or Installation(
            1, False, frozenset({ANALYTICS, BILLING}))
        self.calls = []
        self.fail = None
        # Capability evidence: the permissions the token could not prove.
        self.unmet_grants = lambda token, repository_ids, permissions: []
        self.lock = threading.Lock()

    def installation_for(self, owner):
        return self.installation

    def create_token(self, installation_id, repository_ids, permissions):
        with self.lock:
            self.calls.append((installation_id, list(repository_ids), dict(permissions)))
            n = len(self.calls)
        if self.fail:
            raise self.fail
        return f"ghs_{n}", self.clock() + LIFETIME


def build(*, installation=None, org_boards=frozenset()):
    clock = Clock()
    app = App(clock, installation)
    repos = {ANALYTICS: "me/analytics", BILLING: "me/billing"}
    executions = {
        "exec-analytics": ExecutionScope("analytics", "task", {ANALYTICS: repos[ANALYTICS]}),
        "triage-analytics": ExecutionScope("analytics", "triage", {ANALYTICS: repos[ANALYTICS]}),
        "exec-billing": ExecutionScope("billing", "task", {BILLING: repos[BILLING]}),
    }
    creds = ExecutionCredentials(
        app=app, executions=executions,
        boards={"me/1": BOARD_REF, "me/2": BOARD_REF}, org_boards=org_boards,
        control=CONTROL_REF, secrets=VALUES.__getitem__, clock=clock)
    return creds, app, clock


def test_s12_replacement_is_automatic_and_stays_on_the_one_repository():
    creds, app, clock = build()
    first = creds.acquire("exec-analytics", ANALYTICS, "task")
    clock.now += LIFETIME + 1
    # An expired token is never handed out again: the next acquire replaces it.
    second = creds.acquire("exec-analytics", ANALYTICS, "task")
    third = creds.refresh("exec-analytics", ANALYTICS, "task")
    assert len({first.token, second.token, third.token}) == 3
    assert len(app.calls) == 3
    for _, repository_ids, permissions in app.calls:
        assert repository_ids == [ANALYTICS]
        assert permissions == app.calls[0][2]
    assert second.expires_at > clock.now and third.expires_at > clock.now
    # No App private key anywhere a task could see it.
    for c in (first, second, third):
        assert APP_KEY not in repr(c) and "PRIVATE KEY" not in repr(vars(c))


def test_task_token_covers_its_repository_and_cannot_be_widened_by_the_caller():
    creds, app, _ = build()
    ok = creds.acquire("exec-analytics", ANALYTICS, "task")
    assert ok.token and ok.repository_id in (ANALYTICS, str(ANALYTICS))
    assert app.calls[0][1] == [ANALYTICS]
    # The task can push and open pull requests/issues on that repository.
    assert {k: app.calls[0][2].get(k) for k in ("contents", "pull_requests", "issues")} == {
        "contents": "write", "pull_requests": "write", "issues": "write"}
    assert ok.permissions == app.calls[0][2]
    for claimed in (BILLING, str(BILLING), f"{ANALYTICS},{BILLING}",
                    [ANALYTICS, BILLING], "me/billing", "*", "", None, 0):
        with pytest.raises(CredentialDenied):
            creds.acquire("exec-analytics", claimed, "task")
        with pytest.raises(CredentialDenied):
            creds.refresh("exec-analytics", claimed, "task")
    with pytest.raises(CredentialDenied):
        creds.acquire("exec-unknown", ANALYTICS, "task")
    with pytest.raises(CredentialDenied):  # a task execution is not a triage one
        creds.acquire("exec-analytics", ANALYTICS, "triage")
    # Nothing but the single recorded-scope request ever reached GitHub.
    assert [c[1] for c in app.calls] == [[ANALYTICS]]


def test_triage_gets_a_narrower_permission_set_than_a_task():
    creds, app, _ = build()
    creds.acquire("exec-analytics", ANALYTICS, "task")
    creds.acquire("triage-analytics", ANALYTICS, "triage")
    task, triage = app.calls[0][2], app.calls[1][2]
    assert triage != task
    assert task["contents"] == "write" and task["pull_requests"] == "write"
    assert triage.get("contents") != "write" and triage.get("pull_requests") != "write"


def test_concurrent_refresh_keeps_the_same_repository_and_permission_bounds():
    creds, app, _ = build()
    creds.acquire("exec-analytics", ANALYTICS, "task")
    results = []

    def go():
        results.append(creds.refresh("exec-analytics", ANALYTICS, "task"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 8
    assert len(app.calls) >= 2
    assert {(tuple(c[1]), tuple(sorted(c[2].items()))) for c in app.calls} == {
        ((ANALYTICS,), tuple(sorted(app.calls[0][2].items())))}
    assert all(r.token and r.expires_at for r in results)


def test_refresh_failure_is_visible_and_never_falls_back_to_the_host_login(monkeypatch):
    creds, app, clock = build()
    creds.acquire("exec-analytics", ANALYTICS, "task")
    monkeypatch.setenv("GH_TOKEN", "host-login-token")
    monkeypatch.setenv("GITHUB_TOKEN", "host-login-token")

    def no_shell(*a, **kw):
        raise AssertionError("fell back to the host GitHub login (subprocess)")
    monkeypatch.setattr(subprocess, "run", no_shell)
    monkeypatch.setattr(subprocess, "Popen", no_shell)
    app.fail = RuntimeError("github 503")
    with pytest.raises(TokenGenerationFailed):
        creds.refresh("exec-analytics", ANALYTICS, "task")
    clock.now += LIFETIME + 1  # the old token is now expired: not a fallback either
    with pytest.raises(TokenGenerationFailed):
        creds.acquire("exec-analytics", ANALYTICS, "task")


def test_app_key_board_and_control_credentials_are_unreachable_from_task_and_triage():
    creds, app, _ = build()
    for execution, purpose in (("exec-analytics", "task"), ("triage-analytics", "triage")):
        for asked in ("board", "control", "app-key", "app_private_key", "host"):
            for target in (ANALYTICS, "me/1", "me/2", "infra"):
                with pytest.raises(CredentialDenied):
                    creds.acquire(execution, target, asked)
        issued = creds.acquire(execution, ANALYTICS, purpose)
        text = repr(issued) + repr(vars(issued))
        for secret in (APP_KEY, *VALUES.values()):
            assert secret not in text
    assert all(c[1] == [ANALYTICS] for c in app.calls)


def test_s15_trusted_board_credential_is_reused_across_registered_boards_only():
    creds, app, _ = build()
    a = creds.acquire(TRUSTED, "me/1", "board")
    b = creds.acquire(TRUSTED, "me/2", "board")
    assert a.token == b.token == VALUES[BOARD_REF]
    with pytest.raises(CredentialDenied):
        creds.acquire(TRUSTED, "me/99", "board")  # unregistered board
    for execution in ("exec-analytics", "exec-billing"):
        with pytest.raises(CredentialDenied):
            creds.acquire(execution, "me/1", "board")
    assert app.calls == []  # board access never mints or touches an App token


def test_control_credential_is_distinct_and_trusted_only():
    creds, _, _ = build()
    control = creds.acquire(TRUSTED, "agent-ops-infra", "control")
    assert control.token == VALUES[CONTROL_REF]
    assert control.token != creds.acquire(TRUSTED, "me/1", "board").token
    with pytest.raises(CredentialDenied):
        creds.acquire("exec-billing", "agent-ops-infra", "control")


@pytest.mark.parametrize("installation,error", [
    (None, MissingInstallation),
    (Installation(1, False, frozenset({BILLING})), MissingRepositoryAccess),
    (Installation(1, True, frozenset({ANALYTICS})), OrganizationApprovalPending),
])
def test_api_capability_failures_are_told_apart(installation, error):
    creds, app, _ = build()
    app.installation = installation
    with pytest.raises(error) as seen:
        creds.acquire("exec-analytics", ANALYTICS, "task")
    for other in (MissingInstallation, MissingRepositoryAccess,
                  OrganizationApprovalPending, TokenGenerationFailed):
        if other is not error:
            assert not isinstance(seen.value, other)
    assert str(seen.value)  # names the action the operator must take
    assert app.calls == []


def test_token_generation_failure_is_not_mistaken_for_missing_authorization():
    creds, app, _ = build()
    app.fail = RuntimeError("500 from /access_tokens")
    with pytest.raises(TokenGenerationFailed) as seen:
        creds.acquire("exec-analytics", ANALYTICS, "task")
    assert not isinstance(seen.value, (MissingInstallation, MissingRepositoryAccess,
                                       OrganizationApprovalPending))


def test_organization_board_is_authorized_by_the_app_for_trusted_only():
    creds, app, _ = build(org_boards=frozenset({"acme/7"}))
    board = creds.acquire(TRUSTED, "acme/7", "board")
    assert board.token == "ghs_1" and board.token not in VALUES.values()
    assert app.calls and app.calls[0][1] == []  # an owner-level project token
    assert board.permissions == app.calls[0][2]
    assert app.calls[0][2].get("organization_projects") == "write"
    with pytest.raises(CredentialDenied):
        creds.acquire(TRUSTED, "acme/8", "board")  # unregistered organization board
    for execution in ("exec-analytics", "triage-analytics"):
        with pytest.raises(CredentialDenied):
            creds.acquire(execution, "acme/7", "board")
    # A pending organization approval or missing install is a told-apart blocker.
    app.installation = Installation(1, True, frozenset())
    with pytest.raises(OrganizationApprovalPending):
        creds.acquire(TRUSTED, "acme/7", "board")
    app.installation = None
    with pytest.raises(MissingInstallation):
        creds.acquire(TRUSTED, "acme/7", "board")
    app.installation = Installation(1, False, frozenset())
    app.fail = RuntimeError("500")
    with pytest.raises(TokenGenerationFailed):
        creds.acquire(TRUSTED, "acme/7", "board")


def test_launchers_with_a_scoped_token_deliver_only_gh_token_not_the_host_login():
    import shlex
    from dispatcher import containers
    task = containers.session_cmd("n", "/w", "1g", "1", "claude:sonnet", "x",
                                  clone="/c", scoped_token=True)
    triage = containers.triage_cmd("n", "/c", "/t", "1g", "1", "claude:sonnet",
                                   "/p", scoped_token=True)
    for words in (shlex.split(task), triage):
        joined = " ".join(words)
        assert ".config/gh" not in joined
        assert "GH_TOKEN" in words
        assert not any(w.startswith("GH_TOKEN=") for w in words)  # value never in argv
    # Without a scoped token the shared host login is still what is mounted.
    legacy = containers.session_cmd("n", "/w", "1g", "1", "claude:sonnet", "x", clone="/c")
    assert ".config/gh" in legacy


def test_token_is_requested_for_exactly_the_granted_repository_and_permissions():
    creds, app, _ = build()
    for execution, purpose in (("exec-analytics", "task"), ("triage-analytics", "triage")):
        issued = creds.acquire(execution, ANALYTICS, purpose)
        _, repository_ids, permissions = app.calls[-1]
        assert repository_ids == [ANALYTICS] and BILLING not in repository_ids
        assert issued.permissions == permissions
    # No write grant for the other repository ever existed: its execution is its own scope.
    creds.acquire("exec-billing", BILLING, "task")
    assert app.calls[-1][1] == [BILLING]
    # Missing repository grant stays a blocker and never mints.
    app.installation = Installation(1, False, frozenset({BILLING}))
    before = len(app.calls)
    with pytest.raises(MissingRepositoryAccess):
        creds.refresh("exec-analytics", ANALYTICS, "task")
    assert len(app.calls) == before


def test_unresolved_deployment_grant_is_an_explicit_blocker_and_never_a_token():
    from dispatcher.execution_credentials import UnresolvedGrant
    creds, app, _ = build()
    probed = []

    def unmet_grants(token, repository_ids, permissions):
        probed.append((token, list(repository_ids), dict(permissions)))
        return ["pull_requests"]
    app.unmet_grants = unmet_grants
    for execution, purpose in (("exec-analytics", "task"), ("triage-analytics", "triage")):
        with pytest.raises(UnresolvedGrant) as seen:
            creds.acquire(execution, ANALYTICS, purpose)
        assert "pull_requests" in str(seen.value)
        assert not isinstance(seen.value, TokenGenerationFailed)
        with pytest.raises(UnresolvedGrant):
            creds.refresh(execution, ANALYTICS, purpose)
    # The probe judged the real operations on the one granted repository.
    assert probed and all(p[1] == [ANALYTICS] for p in probed)
    # An unproven token is not cached: once the grant resolves, a new one is minted.
    app.unmet_grants = lambda *a: []
    assert creds.acquire("exec-analytics", ANALYTICS, "task").token
    # Organization board access is blocked the same way.
    creds, app, _ = build(org_boards=frozenset({"acme/7"}))
    app.unmet_grants = lambda *a: ["organization_projects"]
    with pytest.raises(UnresolvedGrant):
        creds.acquire(TRUSTED, "acme/7", "board")


def test_sessions_launch_delivers_the_scoped_token_and_never_the_host_login(
        tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions, session_name
    from tests.test_containers import make_worktree
    wt, clone = make_worktree(tmp_path)
    creds, app, _ = build()
    name = session_name("analytics", 7, "me/analytics")
    creds.executions[name] = ExecutionScope(
        "analytics", "task", {ANALYTICS: "me/analytics"})
    envs, commands = [], []

    class FakeTab:
        alive = True

        def run(self, cmd):
            commands.append(cmd)
            return True

    def ensure(target, tab_name, worktree, env=None, **kw):
        envs.append(dict(env or {}))
        return FakeTab()
    monkeypatch.setattr(herdr.Tab, "ensure", staticmethod(ensure))
    Sessions(clones={"me/analytics": clone}, credentials=creds).resume(
        "analytics", 7, wt, "go", "anthropic/claude-opus-5-5",
        session_id="7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d", repo="me/analytics")
    # The launch asked the service for exactly this execution's one repository.
    assert [c[1] for c in app.calls] == [[ANALYTICS]]
    assert envs[0]["GH_TOKEN"] == "ghs_1"
    assert ".config/gh" not in commands[0] and "ghs_1" not in commands[0]
    # A denied credential stops the launch: no tab command, no host-login fallback.
    commands.clear()
    app.fail = RuntimeError("github 503")
    with pytest.raises(TokenGenerationFailed):
        Sessions(clones={"me/analytics": clone}, credentials=creds).resume(
            "analytics", 7, wt, "go", "anthropic/claude-opus-5-5",
            session_id="7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d", repo="me/analytics")
    assert commands == []


def test_a_token_without_capability_evidence_is_never_handed_out():
    creds, app, _ = build()
    del app.unmet_grants  # an edge that cannot prove what the token may do
    for execution, purpose in (("exec-analytics", "task"), ("triage-analytics", "triage")):
        with pytest.raises(UnresolvedGrant):
            creds.acquire(execution, ANALYTICS, purpose)


def test_board_transport_authorizes_registered_boards_only_and_never_uses_the_host_token(monkeypatch):
    from dispatcher.board import project_env
    creds, _, _ = build()
    monkeypatch.setenv("GH_PROJECT_TOKEN", "host-project-token")
    env = project_env("me/1", credentials=creds)
    assert env["GH_TOKEN"] == VALUES[BOARD_REF]
    with pytest.raises(CredentialDenied):
        project_env("me/99", credentials=creds)  # unregistered: rejected before gh runs


def _assert_commands_consume_the_replacement(command, token_file, tmp_path):
    """The launch command installs the gh/git consumers ahead of the real
    binaries, and the installed gh reads the replacement token (not the stale
    launch GH_TOKEN) on its next invocation. The container's /usr/bin/gh is
    stood in for by a script that reports the token it was given."""
    import shlex
    words = shlex.split(command)
    mounts = [w for w in words if w.endswith(":/opt/agent-ops-auth:ro")]
    assert len(mounts) == 1, "consumers are not mounted into the session"
    scripts = Path(mounts[0].split(":")[0])
    path = [w for w in words if w.startswith("PATH=")][-1][len("PATH="):]  # last -e wins
    assert path.split(":")[0] == "/opt/agent-ops-auth"
    fake = tmp_path / "real-gh"
    fake.write_text('#!/bin/sh\necho "gh:$GH_TOKEN:$GITHUB_TOKEN"\n')
    fake.chmod(0o755)
    wrapper = tmp_path / "gh"
    wrapper.write_text((scripts / "gh").read_text().replace("/usr/bin/gh", str(fake)))
    wrapper.chmod(0o755)
    out = subprocess.run(
        [str(wrapper), "api", "user"], capture_output=True, text=True, check=True,
        env={"GH_TOKEN": "ghs_1", "GH_TOKEN_FILE": str(token_file)})
    assert out.stdout.strip() == "gh:ghs_2:ghs_2"
    # git's credential helper is the same gh consumer, not the shared helper.
    fake_git = tmp_path / "real-git"
    fake_git.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_git.chmod(0o755)
    git = tmp_path / "git"
    git.write_text((scripts / "git").read_text().replace("/usr/bin/git", str(fake_git)))
    git.chmod(0o755)
    argv = subprocess.run([str(git), "fetch"], capture_output=True, text=True,
                          check=True).stdout.split("\n")
    assert "credential.helper=!/opt/agent-ops-auth/gh auth git-credential" in argv


def test_running_session_token_is_replaced_automatically_before_it_expires(
        tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions, RENEW_MARGIN, session_name
    from tests.test_containers import make_worktree
    wt, clone = make_worktree(tmp_path)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    creds, app, clock = build()
    name = session_name("analytics", 7, "me/analytics")
    creds.executions[name] = ExecutionScope(
        "analytics", "task", {ANALYTICS: "me/analytics"})
    commands = []
    monkeypatch.setattr(herdr.Tab, "ensure", staticmethod(
        lambda *a, **kw: type("T", (), {
            "alive": True, "run": lambda s, c: commands.append(c) or True})()))
    # The server answers `tab list` and the session's tab is present.
    monkeypatch.setattr(herdr, "_call", lambda args: {"tabs": [{"label": name}]})
    sessions = Sessions(clones={"me/analytics": clone}, credentials=creds,
                        state_dir=tmp_path / "state")
    sessions.resume("analytics", 7, wt, "go", "anthropic/claude-opus-5-5",
                    session_id="7b0c2a1e-3f4d-4c5b-9a6e-1d2f3a4b5c6d",
                    repo="me/analytics")
    token_file = tmp_path / "state" / "tokens" / name / "gh-token"
    assert token_file.read_text() == "ghs_1"
    # The running task reads its token from a file, not only the launch env:
    # the host token directory is mounted read-only and GH_TOKEN_FILE names
    # the file inside that mount, so a rewrite reaches the running container.
    from dispatcher import containers
    assert f"GH_TOKEN_FILE={containers.TOKEN_FILE}" in commands[0]
    assert f"{token_file.parent}:{containers.TOKEN_DIR}:ro" in commands[0]
    sessions.renew_tokens()
    assert token_file.read_text() == "ghs_1"  # far from expiry: left alone
    clock.now += LIFETIME - RENEW_MARGIN + 1  # the live session nears expiry
    sessions.renew_tokens()
    assert token_file.read_text() == "ghs_2"
    assert [c[1] for c in app.calls] == [[ANALYTICS], [ANALYTICS]]
    _assert_commands_consume_the_replacement(commands[0], token_file, tmp_path)
    # A session whose tab is gone loses its token and is not renewed again.
    monkeypatch.setattr(herdr, "_call", lambda args: {"tabs": []})
    clock.now += LIFETIME
    sessions.renew_tokens()
    assert not token_file.exists()
    assert len(app.calls) == 2


def test_board_operations_refuse_an_unregistered_board_before_gh_runs(tmp_path):
    import json
    from dispatcher import board
    meta = tmp_path / board.META_PATH
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"projectId": "P", "fields": {
        name: {"id": name, "options": {}}
        for name in ("Status", "Impact", "Effort", "Score", "Area")}}))
    creds, _, _ = build()
    calls = []

    def run(*a, **kw):
        calls.append(a)
        raise AssertionError("gh ran for an unregistered board")
    schema = board.Schema("P", "s", "i", "e", "c", "a", {}, {})
    with pytest.raises(CredentialDenied):
        board.read("me/analytics", str(tmp_path), run=run, credentials=creds,
                   key="me/99")
    brd = board.Board(schema=schema, items={}, issues={}, run=run,
                      credentials=creds, key="me/99")
    with pytest.raises(CredentialDenied):
        brd._mutate(board.Item("i", 1, "t", None, None, None, None), {})
    assert calls == []


def test_board_read_refuses_a_registered_key_whose_clone_names_another_board(tmp_path):
    import json
    from dispatcher import board
    meta = tmp_path / board.META_PATH
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"projectId": "OTHER", "fields": {
        name: {"id": name, "options": {}}
        for name in ("Status", "Impact", "Effort", "Score", "Area")}}))
    from tests.test_board import registered_board_gh
    creds, _, _ = build()
    calls = []
    brd = board.read("me/analytics", str(tmp_path), run=registered_board_gh(calls),
                     credentials=creds, key="me/1")  # registered, but the clone names board OTHER
    # The stale clone mapping is refused: only the registered board is resolved and read.
    assert brd.schema.project_id == "REG" and brd.bound == "REG"
    assert "OTHER" not in json.dumps(calls)
