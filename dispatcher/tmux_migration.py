"""One-shot deploy migration: end every live tmux session the pre-herdr
dispatcher left behind and hand its task to the park/resume path, which
resumes it in herdr with `claude --continue` on the next pass (one
interrupted turn per in-flight task, once). Invoked by provision/update.sh
via `dispatcher.main --migrate-tmux` only while `tmux ls` succeeds, so it
is a no-op — and this module a pure deletion — once tmux is gone from the
box. Delete together with the tmux `apt` line in bootstrap.sh."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from dispatcher import triage
from dispatcher.sessions import session_name
from dispatcher.state import TaskState, load_all

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


def _has_session(name: str) -> bool:
    return _run(["tmux", "has-session", "-t", name], 30) == 0


def _kill_session(name: str) -> str:
    rc = _run(["tmux", "kill-session", "-t", name], 30)
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
    session that belongs to a known task, remove both container names,
    and `wake(task, MESSAGE)` for each. Kill a `triage` tmux session and
    re-enqueue the sweep (triage.enqueue — a no-op when a request is
    already pending). Returns one human-readable line per action for the
    updater's log. Best-effort throughout: a tmux/podman failure is
    logged in the returned lines, never raised."""
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
