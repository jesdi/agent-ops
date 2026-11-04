"""Installation tokens a container mounts as its gh config.

A token directory is 0700 and holds one file, hosts.yml (0600), with a token
narrowed to the container's repository. It is mounted read-only at
/root/.config/gh in place of the host's own gh config. This module owns that
file's format, where the directories live under the state dir, and the mint.
The token's expiry is dispatcher state, kept outside the mounted directory.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

from dispatcher import github_app

# What a task session may do on its repository (spec A3): nothing beyond it.
TASK_PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write",
                    "workflows": "write", "actions": "read", "checks": "read",
                    "metadata": "read"}

TOKENS = "tokens"            # state_dir/tokens/<task_key>: a task's mounted directory
EXPIRY = "token-expiry"      # state_dir/token-expiry/<task_key>: its token's expires_at


def _atomic(path: Path, text: str) -> None:
    """`text` at `path` by rename, so a reader sees the old file or the new one whole."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")   # 0600
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def write(directory: Path, token: github_app.Token) -> None:
    _atomic(directory / "hosts.yml",
            f"github.com:\n    oauth_token: {token.token}\n    user: x-access-token\n"
            "    git_protocol: https\n")


def task_dir(state_dir, key: str) -> Path:
    return Path(state_dir) / TOKENS / key


def issue(access: github_app.Access, state_dir, key: str, repo: str) -> str:
    """Mint a token for `repo` with TASK_PERMISSIONS into the task's directory,
    record its expiry, and return the directory. NoAccess when the App is
    unregistered or doesn't reach `repo`; AppError when it errored: a task
    never falls back to the host's credentials."""
    token = access.grants([repo], TASK_PERMISSIONS)[repo.lower()]
    directory = task_dir(state_dir, key)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    write(directory, token)
    expiry = Path(state_dir) / EXPIRY / key
    expiry.parent.mkdir(parents=True, exist_ok=True)
    _atomic(expiry, token.expires_at.isoformat())
    return str(directory)


def expires_at(state_dir, key: str) -> datetime | None:
    """The recorded expiry of the task's token; None when unknown."""
    try:
        return datetime.fromisoformat((Path(state_dir) / EXPIRY / key).read_text().strip())
    except (OSError, ValueError):
        return None
