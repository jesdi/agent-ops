"""Review fixes for ticket 07: empty trusted secrets and service error codes."""
import json
from unittest.mock import Mock

import pytest

from dispatcher import board, credential_service as service
from dispatcher.execution_credentials import (
    TRUSTED, CredentialDenied, CredentialError, ExecutionCredentials,
    MissingInstallation, MissingRepositoryAccess, OrganizationApprovalPending,
    TokenGenerationFailed, UnresolvedGrant)


@pytest.mark.parametrize("secret", ["", "  \n"])
def test_empty_board_secret_fails_before_gh_even_with_host_auth(secret, monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "host-login")
    creds = ExecutionCredentials(app=Mock(), executions={}, boards={"me/1": "op://b"},
                                 control="op://c", secrets=lambda ref: secret)
    with pytest.raises(CredentialError, match="'board'"):
        creds.acquire(TRUSTED, "me/1", "board")
    with pytest.raises(CredentialError, match="'control'"):
        creds.refresh(TRUSTED, "infra", "control")
    with pytest.raises(CredentialError):
        board.project_env("me/1", creds)
    field = {"id": "f", "options": {}}
    (tmp_path / ".backlog").mkdir()
    (tmp_path / ".backlog" / "project-meta.json").write_text(json.dumps({
        "projectId": "P", "fields": {n: field for n in
                                     ("Status", "Impact", "Effort", "Score", "Area")}}))
    run = Mock()
    with pytest.raises(CredentialError):
        board.read("me/r", str(tmp_path), run=run, credentials=creds, key="me/1")
    run.assert_not_called()


def test_default_secret_resolver_never_yields_a_credential():
    creds = ExecutionCredentials(app=Mock(), executions={}, boards={"me/1": "op://b"})
    with pytest.raises(CredentialError):
        creds.acquire(TRUSTED, "me/1", "board")


def test_service_empty_token_is_refused(monkeypatch):
    _respond(monkeypatch, 200, dict(token=" ", repository_id=1, permissions={},
                                    expires_at=1))
    with pytest.raises(CredentialError):
        _client().acquire(TRUSTED, "me/1", "board")


def _client():
    return service.CredentialService("/trusted/service.sock", {"me/analytics": "analytics"})


def _respond(monkeypatch, status, body):
    connection = Mock()
    connection.getresponse.return_value.status = status
    connection.getresponse.return_value.read.return_value = (
        body if isinstance(body, bytes) else json.dumps(body).encode())
    monkeypatch.setattr(service, "_Connection", lambda *a, **kw: connection)


@pytest.mark.parametrize("code, error, hint", [
    ("organization_approval_required", OrganizationApprovalPending, "approve"),
    ("repository_not_granted", MissingRepositoryAccess, "grant"),
    ("installation_missing", MissingInstallation, "install"),
    ("grant_unresolved", UnresolvedGrant, "grant"),
    ("token_generation_failed", TokenGenerationFailed, "generation"),
    ("denied", CredentialDenied, "not authorized"),
])
def test_service_error_codes_become_distinct_actionable_errors(monkeypatch, code, error, hint):
    _respond(monkeypatch, 403, {"code": code, "detail": "acme/analytics (101)"})
    with pytest.raises(error, match=hint) as raised:
        _client().refresh("task-1", "me/analytics", "task")
    assert "acme/analytics (101)" in str(raised.value)


def test_approval_and_missing_grant_are_distinguishable(monkeypatch):
    _respond(monkeypatch, 403, {"code": "organization_approval_required", "detail": "acme"})
    with pytest.raises(CredentialError) as approval:
        _client().refresh("task-1", "me/analytics", "task")
    _respond(monkeypatch, 403, {"code": "repository_not_granted", "detail": "acme/analytics"})
    with pytest.raises(CredentialError) as grant:
        _client().refresh("task-1", "me/analytics", "task")
    assert type(approval.value) is not type(grant.value)


def test_service_error_detail_never_leaks_tokens(monkeypatch):
    _respond(monkeypatch, 403, {"code": "token_generation_failed",
                                "detail": "rejected ghs_abc123SECRET and github_pat_11XYZ_q" + "x" * 500})
    with pytest.raises(TokenGenerationFailed) as raised:
        _client().acquire("task-1", "me/analytics", "task")
    message = str(raised.value)
    assert "SECRET" not in message and "11XYZ" not in message
    assert len(message) < 400


@pytest.mark.parametrize("body", [
    b"not json", b"[]", {"code": "unheard_of", "detail": "x"}, {"detail": "x"},
    {"code": ["organization_approval_required"]}, {"token": "ghs_leak"}])
def test_unknown_or_malformed_error_bodies_stay_generic(monkeypatch, body):
    _respond(monkeypatch, 403, body)
    with pytest.raises(CredentialError, match="HTTP 403") as raised:
        _client().acquire("task-1", "me/analytics", "task")
    assert type(raised.value) is CredentialError
    assert "leak" not in str(raised.value)
