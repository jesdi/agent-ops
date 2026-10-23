"""Owned task sandboxes follow their task's execution accounting: services stop
when the task stops consuming capacity, the slot is only given back once the
stop is confirmed, services restart on resume and everything is removed when
the task is retired. `sandbox` is a TaskSandbox (or any router with its
owns/park/resume/cleanup); `clone_of(task)` is the task's clone, or None."""
from __future__ import annotations

import json
from pathlib import Path

from dispatcher import eventlog
from dispatcher.sandbox import EngineError
from dispatcher.sandbox_runtime import execution_ref
from dispatcher.state import (NO_SLOT, TaskState, allocate_slot, consumes_capacity,
                              hold, load_all, max_slots, release, reserved_slots,
                              task_key)

SETTLED_FILE = "sandbox-settled.json"   # refs whose services are confirmed stopped


def _settled(state_dir) -> set[str]:
    try:
        return set(json.loads((Path(state_dir) / SETTLED_FILE).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def _save_settled(state_dir, refs: set[str]) -> None:
    path = Path(state_dir) / SETTLED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(refs)))
    temporary.replace(path)


def _ref(sandbox, clone_of, task: TaskState) -> str | None:
    clone = clone_of(task)
    ref = execution_ref(clone, task.issue) if clone else None
    return ref if ref and sandbox.owns(ref) else None


def settle(cfg, sandbox, clone_of) -> None:
    """Stop the services of every task that no longer consumes capacity. A
    task whose stop is not confirmed keeps holding a unit until a retry works."""
    settled = _settled(cfg.state_dir)
    before = set(settled)
    for task in load_all(cfg.state_dir):
        ref = _ref(sandbox, clone_of, task)
        if ref is None:
            continue
        if consumes_capacity(task):
            settled.discard(ref)
        elif ref not in settled:
            owner = task_key(task.target, task.issue, task.repo)
            try:
                sandbox.park(ref)
            except (EngineError, OSError) as error:
                eventlog.append_event(cfg.state_dir, "sandbox-stop-failed",
                                      target=task.target, issue=task.issue,
                                      repo=task.repo, stage=task.stage.value,
                                      detail=str(error))
                free = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity),
                                     reserved_slots(cfg.state_dir))
                hold(cfg.state_dir, "task", owner, NO_SLOT if free is None else free)
                continue
            release(cfg.state_dir, "task", owner)
            settled.add(ref)
    if settled != before:
        _save_settled(cfg.state_dir, settled)


def wake(cfg, sandbox, clone_of, task: TaskState) -> bool:
    """Restart the services of a task about to run again; False when they will not start."""
    ref = _ref(sandbox, clone_of, task)
    if ref is None:
        return True
    owner = task_key(task.target, task.issue, task.repo)
    # Durable before anything starts: an interrupted or half-failed resume is
    # then stopped again by settle, and the unit stays held meanwhile.
    _save_settled(cfg.state_dir, _settled(cfg.state_dir) - {ref})
    free = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity),
                         reserved_slots(cfg.state_dir))
    hold(cfg.state_dir, "task", owner, NO_SLOT if free is None else free)
    if not sandbox.resume(ref).ready:
        return False
    release(cfg.state_dir, "task", owner)
    return True


def cleanup(cfg, sandbox, clone_of, task: TaskState) -> bool:
    """Remove the task's retained resources; False (reported) when that failed."""
    owner = task_key(task.target, task.issue, task.repo)
    ref = _ref(sandbox, clone_of, task)
    if ref is None:
        # Resources already gone (possibly an interrupted cleanup): drop any stored hold.
        release(cfg.state_dir, "task", owner)
        return True
    try:
        sandbox.cleanup(ref)
    except (EngineError, OSError) as error:
        eventlog.append_event(cfg.state_dir, "cleanup-failed", target=task.target,
                              issue=task.issue, repo=task.repo, stage=task.stage.value,
                              detail=str(error))
        return False
    release(cfg.state_dir, "task", owner)
    _save_settled(cfg.state_dir, _settled(cfg.state_dir) - {ref})
    return True
