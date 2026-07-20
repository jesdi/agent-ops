"""Exclusive lock shared between the dispatcher pass and update.sh.

provision/update.sh flocks the same file (<state_dir>/convergence.lock)
before swapping code, so a pass never observes a half-updated checkout
(ADR 0001 §3). Blocking acquire: updates take a few seconds at most."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def pass_lock(state_dir: str):
    path = Path(state_dir) / "convergence.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
