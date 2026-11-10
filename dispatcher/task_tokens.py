"""Installation tokens a container mounts as its gh config.

A token directory is 0700 and holds hosts.yml (0600), with a token narrowed
to the container's repository; a task's also holds copies of the host's
commit-signing key and its allowed-signers file (triage never commits). It is mounted read-only at
/root/.config/gh in place of the host's own gh config. This module owns that
file's format, where the directories live under the state dir, and the mint.
The token's expiry is dispatcher state, kept outside the mounted directory.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from dispatcher import github_app

# What a task session may do on its repository (spec A3): nothing beyond it.
TASK_PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write",
                    "workflows": "write", "actions": "read", "checks": "read",
                    "metadata": "read"}

# What a triage session may do on its repository: read, nothing else.
TRIAGE_PERMISSIONS = {"contents": "read", "issues": "read", "pull_requests": "read",
                      "metadata": "read"}

REFRESH = timedelta(minutes=20)   # a task token with less left is re-minted

TOKENS = "tokens"            # state_dir/tokens/<task_key>: a task's mounted directory
EXPIRY = "token-expiry"      # state_dir/token-expiry/<task_key>: its token's expires_at
TRIAGE = "triage-tokens"     # state_dir/triage-tokens/triage-*: a triage run's directory
SIGNING_KEY = "agent-ops-signing-key"   # the commit-signing key a task's directory carries
ALLOWED_SIGNERS = "allowed-signers"     # its trust file, without which %G? reads N, not G


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
    # The session image signs with /root/.config/gh/SIGNING_KEY and trusts
    # /root/.config/gh/ALLOWED_SIGNERS, both inside this mount; re-copied at
    # each mint, so a rotated key lands within REFRESH.
    for name in (SIGNING_KEY, ALLOWED_SIGNERS):
        source = Path.home() / ".config" / "gh" / name
        if source.is_file():
            _atomic(directory / name, source.read_text())
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


def _due(state_dir, key: str, now: datetime) -> bool:
    """Under REFRESH left, or no expiry recorded (the record lost: mint afresh)."""
    expires = expires_at(state_dir, key)
    return expires is None or expires - now < REFRESH


def _prune_records(state_dir, holders: dict[str, str]) -> None:
    records = Path(state_dir) / EXPIRY
    for record in records.iterdir() if records.is_dir() else ():
        if record.name not in holders:
            record.unlink(missing_ok=True)


def sync(access: github_app.Access, state_dir, holders: dict[str, str],
         now: datetime) -> list[str]:
    """Keep a token directory only for `holders` (task key -> its repository):
    any other directory goes with its expiry record, a crash's leftovers too.
    A holder's token with under REFRESH left is re-minted. Returns why each
    refresh that failed waits for the next pass; the current hosts.yml stays
    in place."""
    _prune_records(state_dir, holders)
    root = Path(state_dir) / TOKENS
    waits = []
    for directory in root.iterdir() if root.is_dir() else ():
        key = directory.name
        if key not in holders:
            shutil.rmtree(directory, ignore_errors=True)
        elif _due(state_dir, key, now):
            try:
                issue(access, state_dir, key, holders[key])
            except (github_app.AppError, github_app.NoAccess, OSError) as exc:
                waits.append(f"{key}: {exc}")
    return waits


@contextmanager
def triage(access: github_app.Access, state_dir, repo: str) -> Iterator[str]:
    """A directory holding a read-only token for `repo` alone, for one triage
    run; removed when the run ends, however it ends. NoAccess or AppError when
    there is no token: triage never falls back to the host's gh config."""
    token = access.grants([repo], TRIAGE_PERMISSIONS)[repo.lower()]
    root = Path(state_dir) / TRIAGE
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="triage-", dir=root))   # 0700
    try:
        write(directory, token)
        yield str(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def prune_triage(state_dir) -> None:
    """A killed sweep's leftovers: no triage run is live when a sweep starts."""
    shutil.rmtree(Path(state_dir) / TRIAGE, ignore_errors=True)
