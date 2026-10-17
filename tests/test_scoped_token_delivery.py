from dispatcher import containers


def test_triage_scoped_token_replaces_host_login():
    plain = containers.triage_cmd("n", "/c", "/t", "1g", "1", "claude-opus-5", "/t/p.md")
    scoped = containers.triage_cmd("n", "/c", "/t", "1g", "1", "claude-opus-5", "/t/p.md",
                                   scoped_token=True)
    assert any(a.endswith("/.config/gh:/root/.config/gh:ro") for a in plain)
    assert not any(".config/gh" in a for a in scoped)
    assert scoped[scoped.index("GH_TOKEN") - 1] == "-e"


def test_org_board_uses_app_authorization_and_returned_permissions_are_frozen():
    import pytest
    from dispatcher.execution_credentials import (
        ExecutionCredentials, Installation, TRUSTED, PERMISSIONS)

    class App:
        def installation_for(self, owner):
            return Installation(1, False, frozenset())

        def create_token(self, iid, repos, perms):
            return "tok", 9e12

        def unmet_grants(self, token, repos, perms):
            return []

    creds = ExecutionCredentials(App(), {}, org_boards=frozenset({"acme/3"}))
    cred = creds.acquire(TRUSTED, "acme/3", "board")
    assert cred.token == "tok" and cred.permissions["organization_projects"] == "write"
    with pytest.raises(TypeError):
        cred.permissions["metadata"] = "write"
    with pytest.raises(TypeError):
        PERMISSIONS["task"]["contents"] = "read"


def test_unmet_grant_is_an_explicit_blocker_and_board_never_falls_back():
    import pytest
    from dispatcher import board
    from dispatcher.execution_credentials import (
        CredentialDenied, ExecutionCredentials, ExecutionScope, Installation,
        UnresolvedGrant)

    class App:
        def installation_for(self, owner):
            return Installation(1, False, frozenset({7}))

        def create_token(self, iid, repos, perms):
            return "tok", 9e12

        def unmet_grants(self, token, repos, perms):
            return ["pull_requests"]

    scope = ExecutionScope("p", "task", {7: "o/r"})
    creds = ExecutionCredentials(App(), {"x": scope})
    with pytest.raises(UnresolvedGrant):
        creds.acquire("x", "o/r", "task")  # named claim resolves inside the scope
    with pytest.raises(CredentialDenied):
        creds.acquire("x", "o/other", "task")
    with pytest.raises(CredentialDenied):
        board.project_env("o/9", credentials=creds)


def _creds(clock):
    from dispatcher.execution_credentials import (
        ExecutionCredentials, ExecutionScope, Installation)

    class App:
        n = 0

        def installation_for(self, owner):
            return Installation(1, False, frozenset({7}))

        def create_token(self, iid, repos, perms):
            self.n += 1
            return f"tok{self.n}", clock[0] + 3600

        def unmet_grants(self, token, repos, perms):
            return []

    return ExecutionCredentials(
        App(), {"task-x": ExecutionScope("p", "task", {7: "o/r"})},
        boards={"me/1": "op://b"}, secrets=lambda ref: "board-secret",
        clock=lambda: clock[0])


def test_running_session_gets_a_replacement_token_before_expiry(tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions, RENEW_MARGIN
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(herdr.Tab, "find", staticmethod(lambda name: object()))
    clock = [1000.0]
    creds = _creds(clock)
    sessions = Sessions(state_dir=tmp_path, credentials=creds)
    sessions._deliver_token("task-x", "o/r", creds.refresh("task-x", "o/r", "task"))
    file = tmp_path / "tokens" / "task-x" / "gh-token"
    assert file.read_text() == "tok1"
    sessions.renew_tokens()
    assert file.read_text() == "tok1"  # not near expiry yet
    clock[0] += 3600 - RENEW_MARGIN + 1
    sessions.renew_tokens()
    assert file.read_text() == "tok2"


def test_board_mutation_is_refused_for_an_unregistered_board(monkeypatch):
    import pytest
    from dispatcher import board
    from dispatcher.execution_credentials import CredentialDenied
    creds = _creds([0.0])
    calls = []
    schema = board.Schema("P", "s", "i", "e", "c", "a", {}, {})
    brd = board.Board(schema=schema, items={}, issues={},
                      run=lambda *a, **k: calls.append(a), credentials=creds,
                      key="me/99")
    with pytest.raises(CredentialDenied):
        brd._mutate(board.Item("i", 1, "t", None, None, None, None), {})
    assert calls == []


def test_board_read_refuses_clone_metadata_naming_another_project(tmp_path):
    import json
    import subprocess
    import pytest
    from dispatcher import board
    meta = {"projectId": "OTHER", "fields": {
        n: {"id": n, "options": {}} for n in
        ("Status", "Impact", "Effort", "Score", "Area")}}
    (tmp_path / ".backlog").mkdir()
    (tmp_path / ".backlog" / "project-meta.json").write_text(json.dumps(meta))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = json.dumps({"data": {"repositoryOwner": {"projectV2": {"id": "REG"}}}})
        return subprocess.CompletedProcess(argv, 0, out, "")

    with pytest.raises(board.BoardError):
        board.read("o/r", str(tmp_path), run=run, credentials=_creds([0.0]),
                   key="me/1")
    assert len(calls) == 1  # only the node-id lookup ran


def test_renewal_keeps_credentials_when_herdr_is_unreachable(tmp_path, monkeypatch):
    from dispatcher import herdr
    from dispatcher.sessions import Sessions
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(herdr, "_call", lambda args: None)
    creds = _creds([1000.0])
    sessions = Sessions(state_dir=tmp_path, credentials=creds)
    sessions._deliver_token("task-x", "o/r", creds.refresh("task-x", "o/r", "task"))
    sessions.renew_tokens()
    assert (tmp_path / "tokens" / "task-x" / "gh-token").exists()
    monkeypatch.setattr(herdr, "_call", lambda args: {"tabs": []})
    sessions.renew_tokens()
    assert not (tmp_path / "tokens" / "task-x").exists()
