"""One-shot deploy migration: end every live tmux session the pre-herdr
dispatcher left behind, and hand the tasks the dispatcher actually drives
— the in-flight ones, by `state.consumes_capacity` — to the park/resume
path, which resumes each in herdr with `claude --continue` on the next
pass (one interrupted turn per in-flight task, once). Every other live
session (a PR_OPEN implement session, a FAILED/DONE tombstone, a
gate-parked task with an anomalous live session) is ended and left ended:
`_resume_woken` has no stage filter, so waking those would run
`claude --continue` in a transcript nothing is waiting on — pushing to a
PR under review, or standing a container up outside capacity accounting.
Invoked by provision/update.sh via `dispatcher.main --migrate-tmux` only
while a `task-*`/`triage` tmux session exists, so it is a no-op — and
this module a pure deletion — once tmux is gone from the box. Delete
together with the tmux `apt` line in bootstrap.sh."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from dispatcher import triage
from dispatcher.sessions import session_name
from dispatcher.state import TaskState, consumes_capacity, load_all

MESSAGE = ("Your session was moved from tmux to herdr by a deploy; the "
           "previous turn was interrupted. Continue where you left off.")


def _run(args: list[str], timeout: int) -> int | None:
    """Exit status of a subprocess, or None when the call could not be made
    at all (binary absent, hung server). Never raises: a migration that
    cannot reach tmux or podman degrades to "nothing to migrate" rather
    than failing the deploy pass it runs inside."""
    try:
        return subprocess.run(args, capture_output=True,
                              timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return None


def _target(name: str) -> str:
    """tmux's exact-match target form. A bare `-t <name>` resolves by exact
    name and THEN by prefix, so with only `task-acme-42` alive `has-session
    -t task-acme-4` succeeds and `kill-session -t task-acme-4` kills task
    42 — and this module probes every task in load_all(), tombstones
    included, so a task numbered 4 would silently strand task 42."""
    return f"={name}"


def _has_session(name: str) -> bool:
    return _run(["tmux", "has-session", "-t", _target(name)], 30) == 0


def _kill_session(name: str) -> str:
    rc = _run(["tmux", "kill-session", "-t", _target(name)], 30)
    if rc == 0:
        return f"killed tmux session {name}"
    if rc is None:
        return f"tmux unreachable: session {name} may still be running"
    return f"tmux refused to kill session {name} (exit {rc})"


def _remove_container(name: str) -> str:
    rc = _run(["podman", "rm", "-f", name], 60)
    if rc == 0:
        return f"removed container {name}"
    if rc is None:
        return f"podman unreachable: container {name} may still be running"
    return f"no container {name} to remove"


def migrate(state_dir: str | Path,
            wake: Callable[[TaskState, str], None]) -> list[str]:
    """Kill every `task-<target>-<issue>` / legacy `task-<issue>` tmux
    session that belongs to a known task and remove both container names.
    Then, and only for a task the dispatcher would still drive —
    `state.consumes_capacity`: an in-flight stage, unparked or
    PARK_LOGIN — `wake(task, MESSAGE)` so the next pass resumes it in
    herdr. Anything else (PR_OPEN, tombstones, gate-parked) is ended and
    logged as "not resumed": resuming it would restart a claude nothing is
    waiting on. Kill a `triage` tmux session and re-enqueue the sweep
    (triage.enqueue — a no-op when a request is already pending). Returns
    one human-readable line per action for the updater's log. Best-effort
    throughout: a tmux/podman failure is logged in the returned lines,
    never raised."""
    lines: list[str] = []
    for task in load_all(state_dir):
        name, legacy = session_name(task.target, task.issue), f"task-{task.issue}"
        live = [n for n in (name, legacy) if _has_session(n)]
        if not live:
            continue
        # Kill first, wake last: the wake is what queues the task for a
        # herdr resume, and it must never be queued while its tmux session
        # and container are still up.
        lines.extend(_kill_session(n) for n in live)
        lines.extend(_remove_container(n) for n in (name, legacy))
        if not consumes_capacity(task):
            lines.append(f"ended {name} (not resumed: stage "
                         f"{task.stage.value}"
                         + (f" park {task.park}" if task.park else "") + ")")
            continue
        wake(task, MESSAGE)
        lines.append(f"handed {name} to the park/resume path "
                     f"(resumes in herdr on the next pass)")

    if _has_session(triage.TAB_LABEL):
        lines.append(_kill_session(triage.TAB_LABEL))
        # The sweep has no state file and no park, so it cannot be woken —
        # re-request it instead and let the next pass launch it in herdr.
        lines.append("re-enqueued the triage sweep"
                     if triage.enqueue(state_dir)
                     else "triage sweep already pending; not re-enqueued")
    return lines
