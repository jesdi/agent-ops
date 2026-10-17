"""Exclusive lock shared between the dispatcher pass and update.sh.

agent-ops-infra/provision/update.sh flocks the same file (<state_dir>/convergence.lock)
before swapping code, so a pass never observes a half-updated checkout
(ADR 0001 §3). Blocking acquire: updates take a few seconds at most. The
console takes it too, with a time limit, to store model settings between
passes."""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def pass_lock(state_dir: str, timeout: float | None = None,
              name: str = "convergence.lock"):
    """Hold the lock. `timeout` None waits as long as it takes; else raise
    TimeoutError when the lock is still held after `timeout` seconds."""
    path = Path(state_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        _acquire(fh.fileno(), timeout)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _acquire(fd: int, timeout: float | None) -> None:
    if timeout is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"convergence.lock still held after {timeout}s") from None
            time.sleep(0.1)


def claim_lock(state_dir: str):
    """The lock around one claim's decision and its saved state: a separate
    file, since the pass already holds convergence.lock."""
    return pass_lock(state_dir, name="claims.lock")
