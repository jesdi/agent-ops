"""Dispatcher-only Unix-socket contract with the infrastructure credential service.

The socket is never mounted in execution containers. The service persists
execution registrations, resolves repository names to GitHub IDs, and enforces
ExecutionCredentials authorization and capability checks before returning tokens.

A refusal (any non-200) may carry a JSON body `{"code": ..., "detail": ...}`
with a code from ERROR_CODES and a short detail that never contains a token;
the client raises the matching refusal. Other bodies give a generic error.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time

from dispatcher.execution_credentials import (
    Credential, CredentialDenied, CredentialError, MissingInstallation,
    MissingRepositoryAccess, MissingSecretReference, OrganizationApprovalPending, TokenGenerationFailed,
    UnresolvedGrant)

# Non-200 body `{"code": ..., "detail": ...}` -> the distinct refusal it names.
ERROR_CODES = {
    "denied": (CredentialDenied, "caller is not authorized for this credential"),
    "installation_missing": (MissingInstallation, "install the GitHub App for the owner"),
    "repository_not_granted": (MissingRepositoryAccess,
                               "grant the App installation access to the repository"),
    "organization_approval_required": (OrganizationApprovalPending,
                                       "an organization administrator must approve the App"),
    "grant_unresolved": (UnresolvedGrant, "deployment grant unresolved"),
    "token_generation_failed": (TokenGenerationFailed, "GitHub token generation failed"),
}
_TOKEN = re.compile(r"(gh[pousr]_|github_pat_)\w+")


def _refusal(operation: str, status: int, data: bytes) -> CredentialError:
    try:
        body = json.loads(data)
        if body["code"] == "missing_secret_reference":  # names only, never values
            return MissingSecretReference(map(str, body["references"]))
        error, hint = ERROR_CODES[body["code"]]
        detail = _TOKEN.sub("[redacted]", str(body.get("detail", "")))[:200]
    except (ValueError, TypeError, KeyError, AttributeError):
        return CredentialError(f"credential service refused {operation}: HTTP {status}")
    return error(f"credential service refused {operation}: {hint}: {detail}")


class _Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.host)


class CredentialService:
    clock = staticmethod(time.time)

    def __init__(self, path: str, repositories: dict[str, str]):
        self.path = path
        self.repositories = repositories

    def _request(self, operation: str, body: dict) -> dict:
        connection = _Connection(self.path, timeout=30)
        try:
            connection.request("POST", f"/v1/{operation}", json.dumps(body),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise _refusal(operation, response.status, data)
            return json.loads(data)
        finally:
            connection.close()

    def register_execution(self, execution_id: str, repository: str, purpose: str):
        if repository not in self.repositories or purpose not in {"task", "triage"}:
            raise CredentialDenied("execution repository/purpose is not configured")
        self._request("executions/register", {
            "execution_id": execution_id, "project": self.repositories[repository],
            "repositories": [repository], "purpose": purpose})

    def project_env(self, execution_id: str) -> dict[str, str]:
        """The resolved secret environment of the execution's own project; the
        service refuses with the unresolvable references, so a launch fails closed."""
        return self._request("executions/project-env",
                             {"execution_id": execution_id})["env"]

    def _credential(self, operation, execution_id, repository_id, purpose):
        data = self._request(operation, {"execution_id": execution_id,
                            "repository_id": repository_id, "purpose": purpose})
        return Credential(data["token"], str(data["repository_id"]), purpose,
                          data["permissions"], data["expires_at"])

    def acquire(self, execution_id, repository_id, purpose):
        return self._credential("acquire", execution_id, repository_id, purpose)

    def refresh(self, execution_id, repository_id, purpose):
        return self._credential("refresh", execution_id, repository_id, purpose)


def configured_credentials(cfg):
    """Default to scoped access; legacy migration requires explicit opt-in.

    Legacy mode is only for old targets while replacement grants are verified.
    New project configuration must always use the trusted service. Service
    failures propagate; they never select legacy mode automatically.
    """
    if os.environ.get("AGENT_OPS_LEGACY_CREDENTIALS") == "1":
        if cfg.projects:
            raise CredentialDenied("legacy credentials cannot launch registered projects")
        return None
    repositories = {t.repo: t.name for t in cfg.targets}
    repositories.update({r.repo: p.key for p in cfg.projects for r in p.repositories})
    return CredentialService(os.environ.get("AGENT_OPS_CREDENTIAL_SOCKET",
                             "/run/agent-ops-credentials/dispatcher.sock"), repositories)
