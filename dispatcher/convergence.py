"""Exclusive lock shared between the dispatcher pass and update.sh.

agent-ops-infra/provision/update.sh flocks the same file (<state_dir>/convergence.lock)
before swapping code, so a pass never observes a half-updated checkout
(ADR 0001 §3). Blocking acquire: updates take a few seconds at most. The
console takes it too, with a time limit, to store model settings between
passes."""
from __future__ import annotations

from dispatcher.state import state_lock


def pass_lock(state_dir: str, timeout: float | None = None):
    """Hold the lock. `timeout` None waits as long as it takes; else raise
    TimeoutError when the lock is still held after `timeout` seconds."""
    return state_lock(state_dir, "convergence.lock", timeout)
