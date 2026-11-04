"""The credential of a pass's host GitHub work (fetch, PR polling, labels, issues).

`begin(repos_by_project, app)` runs once per pass and mints one token per
installation and project. `env(repo)` then answers every host call: the
installation token's env, None (the stored auth) for a repository the
installation doesn't reach or with no App, and HostWait, never the stored auth,
when the App errored for that repository: its host work waits for the next pass.
"""
from __future__ import annotations

import logging
import os

from dispatcher import github_app

log = logging.getLogger(__name__)

PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write",
               "actions": "read", "statuses": "read"}


class HostWait(OSError):
    """The App errored for this repository: wait for the next pass."""


# git asks `gh` for its credential, which answers with GH_TOKEN: a stored helper never wins.
_GIT_AS_GH = {"GIT_CONFIG_COUNT": "2",
              "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
              "GIT_CONFIG_KEY_1": "credential.helper",
              "GIT_CONFIG_VALUE_1": "!gh auth git-credential"}

_env: dict[str, dict | None | HostWait] = {}   # lowercased repo -> env, None, or why it waits


def begin(projects: list[list[str]], app) -> None:
    _env.clear()
    if app is None:
        return
    try:
        if not app.registered():
            return
    except github_app.AppError as exc:
        log.warning("GitHub App unavailable, host work waits: %s", exc)
        _env.update({r.lower(): HostWait(str(exc)) for repos in projects for r in repos})
        return
    for repos in projects:
        _project(repos, app)


def _project(repos: list[str], app) -> None:
    try:
        by_install: dict = {}
        for repo in repos:
            if inst := app.installation(repo):
                by_install.setdefault(inst, []).append(repo)
        for inst, names in by_install.items():
            token = app.mint(inst, names, PERMISSIONS).token
            _env.update({n.lower(): {**os.environ, **_GIT_AS_GH, "GH_TOKEN": token} for n in names})
    except github_app.AppError as exc:
        log.warning("GitHub App error for %s, its host work waits for the next pass: %s",
                    ", ".join(repos), exc)
        _env.update({r.lower(): HostWait(f"{r}: {exc}") for r in repos})


def env(repo: str) -> dict | None:
    found = _env.get(repo.lower())
    if isinstance(found, HostWait):
        raise found
    return found


def kw(repo: str) -> dict:
    """`env(repo)` as keyword arguments: empty for the stored auth."""
    found = env(repo)
    return {} if found is None else {"env": found}
