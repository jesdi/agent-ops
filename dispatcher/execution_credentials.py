"""Trusted credential service: scoped, expiring GitHub App tokens per execution.

Seam CREDENTIAL -- `ExecutionCredentials.acquire/refresh(execution_id,
repository_id, purpose)`. 
Contract (see tests/test_execution_credentials_acceptance.py):
- `executions` maps a trusted execution id to its recorded `ExecutionScope`.
  The caller identity is the execution id; the repository a token may cover
  is only ever a member of the recorded scope, never the caller's claim.
- `app` is the GitHub App edge (`installation_for`, `create_token`); the
  private key lives behind it and never reaches a `Credential`.
- Purposes "task" and "triage" are issued to executions of that kind.
  Purposes "board" and "control" are issued only to `TRUSTED`, from the
  1Password references in `boards` / `control`, resolved by `secrets`.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

TRUSTED = "dispatcher"

# Permission bounds per purpose; triage never writes code or pull requests.
PERMISSIONS = MappingProxyType({
    "task": MappingProxyType({"contents": "write", "pull_requests": "write",
                              "issues": "write", "metadata": "read"}),
    "triage": MappingProxyType({"contents": "read", "pull_requests": "read",
                                "issues": "read", "metadata": "read"}),
})
# Organization boards are authorized by the App itself, not a stored secret.
ORG_BOARD_PERMISSIONS = MappingProxyType({"organization_projects": "write",
                                          "metadata": "read"})


class CredentialError(Exception):
    """Base of every refusal or failure of the credential service."""


class CredentialDenied(CredentialError):
    """The caller may not hold a credential for that repository/purpose."""


class MissingInstallation(CredentialError):
    """The App is not installed for the repository owner."""


class MissingRepositoryAccess(CredentialError):
    """The installation exists but was not granted this repository."""


class OrganizationApprovalPending(CredentialError):
    """An organization administrator has not approved the installation."""


class UnresolvedGrant(CredentialError):
    """The token was minted but the App lacks a required write capability
    (repository contents/pull requests, or organization projects)."""


class TokenGenerationFailed(CredentialError):
    """GitHub refused or failed to mint the token (initial issue or refresh)."""


@dataclass(frozen=True)
class ExecutionScope:
    project: str
    kind: str  # "task" | "triage"
    repositories: Mapping[int, str]  # repository id -> "owner/name"


@dataclass(frozen=True)
class Installation:
    id: int
    pending_approval: bool
    repository_ids: frozenset[int]


@dataclass(frozen=True)
class Credential:
    token: str
    repository_id: str
    purpose: str
    permissions: Mapping[str, str]
    expires_at: float

    def __post_init__(self):
        # An empty GH_TOKEN lets gh fall back to GITHUB_TOKEN or the host login.
        if not isinstance(self.token, str) or not self.token.strip():
            raise CredentialError(f"no usable {self.purpose!r} token for "
                                  f"{self.repository_id!r}: check its configured credential")


class AppApi(Protocol):
    def installation_for(self, owner: str) -> Installation | None: ...

    def create_token(self, installation_id: int, repository_ids: list[int],
                     permissions: dict[str, str]) -> tuple[str, float]: ...

    # Optional capability probe: the permissions among `permissions` the token
    # cannot really exercise on `repository_ids` (empty list: organization
    # level). Required: a token the App cannot prove is never handed out.
    # def unmet_grants(self, token: str, repository_ids: list[int],
    #                  permissions: dict[str, str]) -> list[str]: ...


def _claimed_id(claim, scope: ExecutionScope):
    """The claim as a repository id: digits, or an "owner/name" found in the
    recorded scope (never resolved outside it)."""
    if not isinstance(claim, str):
        return claim
    if claim.isdigit():
        return int(claim)
    return next((i for i, n in scope.repositories.items() if n == claim), claim)


@dataclass
class ExecutionCredentials:
    app: AppApi
    executions: Mapping[str, ExecutionScope]
    boards: Mapping[str, str] = field(default_factory=dict)  # "owner/number" -> op:// ref
    org_boards: frozenset[str] = frozenset()  # "org/number" authorized via the App
    control: str = ""  # op:// ref of the infrastructure credential
    secrets: Callable[[str], str] = lambda ref: ""
    clock: Callable[[], float] = time.time

    def __post_init__(self):
        self._lock = threading.Lock()
        self._issued: dict[tuple[str, int, str], Credential] = {}

    def acquire(self, execution_id: str, repository_id: str | int,
                purpose: str) -> Credential:
        if execution_id == TRUSTED:
            return self._trusted(repository_id, purpose)
        key = self._authorize(execution_id, repository_id, purpose)
        with self._lock:
            held = self._issued.get(key)
            if held and held.expires_at > self.clock():
                return held
            return self._mint(key)

    def refresh(self, execution_id: str, repository_id: str | int,
                purpose: str) -> Credential:
        if execution_id == TRUSTED:
            return self._trusted(repository_id, purpose)
        key = self._authorize(execution_id, repository_id, purpose)
        with self._lock:
            return self._mint(key)

    def _trusted(self, target, purpose: str) -> Credential:
        if purpose == "board" and isinstance(target, str) and target in self.org_boards:
            return self._mint_org_board(target)
        if purpose == "board" and isinstance(target, str) and target in self.boards:
            ref = self.boards[target]
        elif purpose == "control" and self.control:
            ref = self.control
        else:
            raise CredentialDenied(f"no trusted {purpose!r} credential for {target!r}")
        return Credential(self.secrets(ref), str(target), purpose, {}, float("inf"))

    def _mint_org_board(self, board: str) -> Credential:
        owner = board.split("/")[0]
        installation = self._installation(owner)
        permissions = dict(ORG_BOARD_PERMISSIONS)
        token, expires_at = self._create(installation.id, [], permissions)
        return Credential(token, board, "board", MappingProxyType(permissions), expires_at)

    def _create(self, installation_id: int, repository_ids: list[int],
                permissions: dict[str, str]) -> tuple[str, float]:
        try:
            token, expires_at = self.app.create_token(
                installation_id, repository_ids, dict(permissions))
        except Exception as exc:  # never fall back to the host GitHub login
            raise TokenGenerationFailed(f"GitHub token generation failed: {exc}") from exc
        probe = getattr(self.app, "unmet_grants", None)
        unmet = (probe(token, repository_ids, dict(permissions)) if probe
                 else ["capability evidence (App has no probe)"])
        if unmet:
            raise UnresolvedGrant(f"deployment grants unresolved: {', '.join(unmet)}")
        return token, expires_at

    def _installation(self, owner: str) -> Installation:
        installation = self.app.installation_for(owner)
        if installation is None:
            raise MissingInstallation(f"install the GitHub App for {owner!r}")
        if installation.pending_approval:
            raise OrganizationApprovalPending(
                f"an administrator of {owner!r} must approve the App installation")
        return installation

    def _authorize(self, execution_id: str, repository_id, purpose: str):
        scope = self.executions.get(execution_id)
        if scope is None or purpose not in PERMISSIONS or purpose != scope.kind:
            raise CredentialDenied(f"{execution_id!r} may not hold a {purpose!r} credential")
        # The recorded scope decides; the caller's claim is only checked against it.
        claimed = _claimed_id(repository_id, scope)
        if type(claimed) is not int or claimed not in scope.repositories:
            raise CredentialDenied(f"{execution_id!r} is not scoped to {repository_id!r}")
        return execution_id, claimed, purpose

    def _mint(self, key) -> Credential:
        execution_id, repository_id, purpose = key
        owner = self.executions[execution_id].repositories[repository_id].split("/")[0]
        installation = self._installation(owner)
        if repository_id not in installation.repository_ids:
            raise MissingRepositoryAccess(
                f"grant the App installation access to repository {repository_id}")
        permissions = dict(PERMISSIONS[purpose])
        token, expires_at = self._create(installation.id, [repository_id], permissions)
        credential = Credential(token, str(repository_id), purpose, MappingProxyType(permissions), expires_at)
        self._issued[key] = credential
        return credential
