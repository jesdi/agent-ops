"""The credential of a pass's host GitHub work (fetch, PR polling, labels, issues).

`begin(repos_by_project, access)` runs once per pass and mints one token per
installation and project. The HostAuth it returns answers every host call: the
installation token's env, None (the stored auth) for a repository the
installation doesn't reach or with no App, and HostWait, never the stored auth,
when the App errored for that repository: its host work waits for the next pass.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dispatcher import github_app

log = logging.getLogger(__name__)

PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write",
               "actions": "read", "statuses": "read"}


class HostWait(Exception):
    """The App errored for this repository: its host work waits for the next pass.
    Not an OSError: a handler of a failed call must never read it as one."""


# git asks `gh` for its credential, which answers with GH_TOKEN: a stored helper never wins.
_GIT_AS_GH = {"GIT_CONFIG_COUNT": "2",
              "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
              "GIT_CONFIG_KEY_1": "credential.helper",
              "GIT_CONFIG_VALUE_1": "!gh auth git-credential"}


@dataclass(frozen=True)
class HostAuth:
    """One pass's host credentials, by lowercased repository."""
    tokens: dict[str, str] = field(default_factory=dict)   # its installation token
    waits: dict[str, str] = field(default_factory=dict)    # why its host work waits

    def env(self, repo: str) -> dict | None:
        """The env of a host call on `repo`; None: the stored auth."""
        key = repo.lower()
        if key in self.waits:
            raise HostWait(self.waits[key])
        token = self.tokens.get(key)
        return None if token is None else {**os.environ, **_GIT_AS_GH, "GH_TOKEN": token}

    def kw(self, repo: str) -> dict:
        """`env(repo)` as subprocess keyword arguments: empty for the stored auth."""
        found = self.env(repo)
        return {} if found is None else {"env": found}


STORED = HostAuth()   # no App: every repository on the stored auth


def begin(projects: list[list[str]], access: github_app.Access | None) -> HostAuth:
    if access is None:
        return STORED
    tokens, waits = {}, {}
    for repos in projects:
        try:
            granted = access.grants(repos, PERMISSIONS, reached_only=True)
        except github_app.AppError as exc:
            log.warning("GitHub App error for %s, its host work waits for the next pass: %s",
                        ", ".join(repos), exc)
            waits.update({r.lower(): f"{r}: {exc}" for r in repos})
            continue
        tokens.update({repo: t.token for repo, t in granted.items()})
    return HostAuth(tokens, waits)
