"""Session layer: one session per task (task-<target>-<issue>), each stage
a fresh `podman run … <cli>` inside it, the CLI being the stage model's
runtime (claude or codex; see runtimes.py). The session is a herdr tab —
`Tab(session_name(target, issue))` in the workspace labelled `<target>` —
whose root pane hosts the `podman run`; the container is the isolation
layer. Tab and container die together at park and are recreated together
at resume (`claude --resume <id>` / `codex resume <id>` read the recorded
conversation from the mounted runtime home). Fresh launches discard the
task's state-dir session record before the tab starts.

Tabs are resolved by label on every call and never persisted, so a herdr
restart or renumbering cannot strand a task. Every herdr failure degrades
instead of raising: is_alive → False, captures → "", idle_seconds → None,
mutations best-effort — except a launch, which raises (see _launch)."""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
import os
from pathlib import Path
from typing import Mapping

from dispatcher import containers, herdr, workspace
from dispatcher.models import Entry
from dispatcher.runtimes import runtime_for
from dispatcher.state import (clear_session, issue_execution_token,
                              owns_task_session, task_key)


def session_name(target: str, issue: int, repo: str = "") -> str:
    """The execution label. An empty repo keeps `task-<target>-<issue>`. A
    repository adds a digest of its full task key, so two repositories of one
    target never share a tab, container or snapshot, and dotted names
    (`a.b` + `c` against `a` + `b.c`) cannot collide. The readable part uses
    only Podman name characters ([a-zA-Z0-9_.-]): `~` becomes `.`."""
    key = task_key(target, issue, repo)
    if not repo:
        return f"task-{key}"
    return f"task-{key.replace('~', '.')}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def podman_cmd(target: str, issue: int, worktree: str, memory: str, cpus: str,
               model: str, args: str, effort: str = "",
               second: Entry | None = None, clone: str = "",
               repo: str = "") -> str:
    return containers.session_cmd(session_name(target, issue, repo), worktree, memory,
                                  cpus, model, args, effort=effort,
                                  second=second, clone=clone, issue=issue)


# The deployed checkout this module is imported from.
RUNTIME_CHECKOUT = Path(__file__).resolve().parent.parent


# The idle time of a session that is not `working` but whose status just
# changed, or whose recorded change lies in the future (clock skew): small,
# yet never the 0.0 of working. An unreadable clock is None, not this.
_MIN_IDLE = 0.001


class Sessions:
    """The dispatcher's and the console's one session interface. Every
    operation resolves the tab by label; dry-run short-circuits each
    mutation and each capture before the server is asked."""

    def __init__(self, dry_run: bool = False, memory: str = "2g",
                 cpus: str = "2", state_dir: str | Path | None = None,
                 clones: Mapping[str, str] | None = None):
        self.dry_run = dry_run
        self.memory = memory
        self.cpus = cpus
        self.state_dir = Path(state_dir) if state_dir else None
        # target name or "owner/name" repository -> its configured clone
        # path: the only source of the clone a session's container mounts.
        # Without an entry nothing is launched.
        self.clones = clones if clones is not None else {}

    def _legacy_owned(self, target: str, issue: int, repo: str) -> bool:
        """A live unqualified `task-<T>-<N>` session belongs to `repo` only
        when its launch record says so; it is never renamed underneath it."""
        return bool(repo) and self.state_dir is not None and owns_task_session(
            self.state_dir, target, issue, repo)

    def _tab(self, target: str, issue: int, repo: str = "") -> herdr.Tab | None:
        tab = herdr.Tab.find(session_name(target, issue, repo))
        if tab is None and self._legacy_owned(target, issue, repo):
            tab = herdr.Tab.find(session_name(target, issue))
        return tab

    def is_alive(self, target: str, issue: int, repo: str = "") -> bool:
        """Alive means the tab exists AND its shell is busy — a CLI that
        has exited back to the host shell reads dead (the crash path owns
        it), and a tab restored by a herdr server restart reads dead.
        `main._inject_login_code` still re-verifies the prompt with
        `capture_tail` before typing."""
        tab = self._tab(target, issue, repo)
        return tab is not None and tab.alive

    def _launch(self, target: str, issue: int, worktree: str, model: str,
                args: str, effort: str = "",
                second: Entry | None = None, repo: str = "") -> None:
        name = session_name(target, issue, repo)
        # The clone comes from the target's configuration, never from the
        # worktree's `.git` pointer, which the session can rewrite.
        # A repository's own entry wins over its target's, so two
        # repositories of one project never share a clone.
        clone = self.clones.get(repo) or self.clones.get(target)
        if not clone:
            raise RuntimeError(f"no configured clone for target {target!r}: "
                               f"nothing is launched for {name}")
        # The deployed runtime checkout is never a task's clone: task edits
        # must not change the code the dispatcher runs.
        if Path(clone).resolve() == RUNTIME_CHECKOUT:
            raise RuntimeError(f"clone of {target!r} is the runtime checkout "
                               f"{RUNTIME_CHECKOUT}: nothing is launched for {name}")
        # A checkout with no `.git` entry is gone or half-removed. Only its
        # presence is checked: what it says is the session's to write.
        if not os.path.lexists(Path(worktree) / ".git"):
            raise FileNotFoundError(f"{worktree}/.git: not a git worktree (any more)")
        # Build the command FIRST, and write into the worktree before
        # Tab.ensure: a raise (a path the command cannot carry, a vanished
        # worktree: the case _fail_task_crash exists for) then leaves no
        # workspace and no empty tab behind for a task that will never launch.
        runtime = runtime_for(model)
        cmd = podman_cmd(target, issue, worktree, self.memory, self.cpus,
                         model, args, effort=effort,
                         second=second, clone=clone, repo=repo)
        workspace.install_stop_hook(worktree)
        if repo and self.state_dir is not None:
            workspace.bind_execution(worktree, repo, issue_execution_token(
                self.state_dir, target, issue, repo))
        tab = herdr.Tab.ensure(
            target, name, worktree,
            # herdr's hint for detecting an agent behind a wrapper (podman
            # here). Tab-level env so the shell inherits it into every launch
            # and it never appears in the typed command; a herdr concern, so
            # it lives here and never in containers.session_cmd — the
            # headless triage container shares that module and must NOT read
            # as an agent.
            env={"HERDR_AGENT": runtime.herdr_agent})
        # Raise, never return quietly: the caller would record a stage that
        # never started, and the next pass would report a phantom crash with
        # nothing to show (#363). A raise fails the task with this reason.
        if tab is None:
            raise RuntimeError(f"herdr gave no tab for {name}"
                               " — server down or out of step with the CLI?")
        if not tab.run(cmd):
            raise RuntimeError(f"herdr refused the launch command for {name}")

    def spawn_stage(self, target: str, issue: int, worktree: str, prompt: str,
                    stage_name: str, model: str, effort: str = "",
                    second: Entry | None = None, repo: str = "") -> None:
        """Start a fresh conversation; real launches require state_dir."""
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' on {model} in session "
                  f"{session_name(target, issue, repo)} at {worktree}")
            return
        if self.state_dir is None:
            raise ValueError("state_dir is required for a fresh session launch")
        clear_session(self.state_dir, target, issue, repo=repo)
        workspace.write_worktree_file(worktree, ".agent",
                                      f"prompt-{stage_name}.md", prompt)
        self._launch(target, issue, worktree, model,
                     f'"$(cat .agent/prompt-{stage_name}.md)"', effort=effort,
                     second=second, repo=repo)

    def resume(self, target: str, issue: int, worktree: str, message: str,
               model: str, effort: str = "", second: Entry | None = None,
               *, session_id: str, repo: str = "") -> None:
        if self.dry_run:
            print(f"[dry-run] resume {session_name(target, issue, repo)} on {model} "
                  f"at {worktree}")
            return
        runtime = runtime_for(model)
        self._launch(target, issue, worktree, model,
                     runtime.resume(session_id, shlex.quote(message)), effort=effort,
                     second=second, repo=repo)

    def capture_tail(self, target: str, issue: int, lines: int = 25,
                     repo: str = "") -> str:
        if self.dry_run:
            return ""
        tab = self._tab(target, issue, repo)
        text = tab.read("visible", lines) if tab else None
        if text is None:
            return ""
        return "\n".join(text.rstrip().splitlines()[-lines:])

    def capture_history(self, target: str, issue: int, lines: int = 2000,
                        repo: str = "") -> str:
        """Console-owned pane history (true scrollback, unlike capture_tail —
        the dispatcher's stall/login classification input). recent-unwrapped
        joins soft wraps, so the console renders the lines the CLI drew.
        Degrades to '' so a wedged server never 500s the history view."""
        if self.dry_run:
            return ""
        tab = self._tab(target, issue, repo)
        text = tab.read("recent-unwrapped", lines) if tab else None
        return "" if text is None else text.rstrip()

    def idle_seconds(self, target: str, issue: int, repo: str = "") -> float | None:
        """Seconds the session has been static; None = unknown (dry-run,
        herdr error) — callers must not treat it as stalled. Computed from
        the agent lifecycle, not screen activity: `working` is never idle,
        however long it lasts; any other status (idle / blocked / done /
        unknown, or no agent at all — a busy shell that has not reached
        the CLI yet, podman still starting or the wrapper still running)
        accumulates from the moment herdr last changed its mind. herdr
        exposes the transition counter but no timestamp, so the moment is
        remembered in a sidecar keyed by (seq, status).

        0.0 means herdr says `working` and nothing else: every other status
        returns a strictly positive number (_MIN_IDLE at the least), so a
        caller can read idle 0 as "working" (machine._poke_actions)."""
        state = self.agent_state(target, issue, repo)
        if state is None:
            return None
        status, seq = state
        if status == "working":
            return 0.0
        return self._since_change(target, issue, status, seq, repo)

    def agent_state(self, target: str, issue: int, repo: str = "") -> tuple[str, int] | None:
        """herdr's (agent status, state-change counter) for the task's tab;
        None when herdr cannot be asked (dry-run, no tab, server error)."""
        if self.dry_run:
            return None
        tab = self._tab(target, issue, repo)
        return tab.agent_state() if tab else None

    def _sidecar(self, target: str, issue: int, repo: str = "") -> Path | None:
        if self.state_dir is None:
            return None
        return (self.state_dir / "herdr-status"
                / f"{session_name(target, issue, repo)}.json")

    def _since_change(self, target: str, issue: int, status: str,
                      seq: int, repo: str = "") -> float | None:
        path = self._sidecar(target, issue, repo)
        if path is None:
            return None
        now = time.time()
        try:
            try:
                stored = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                stored = None  # absent or corrupt: start the clock afresh
            if (isinstance(stored, dict) and stored.get("seq") == seq
                    and stored.get("status") == status):
                return max(_MIN_IDLE, now - float(stored["since"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"seq": seq, "status": status, "since": now}))
            return _MIN_IDLE
        except (OSError, KeyError, TypeError, ValueError):
            return None  # unknown, never a stale number

    def forget_status(self, target: str, issue: int, repo: str = "") -> None:
        path = self._sidecar(target, issue, repo)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def send_text(self, target: str, issue: int, text: str, repo: str = "") -> bool:
        """Type text into the live pane as-is (login-code injection, pokes),
        then Enter as a separate key. True when both were delivered."""
        if self.dry_run:
            print(f"[dry-run] send text to {session_name(target, issue, repo)}")
            return False
        tab = self._tab(target, issue, repo)
        return tab is not None and tab.send_text(text) and tab.send_keys("enter")

    def _snapshot(self, target: str, issue: int, repo: str = "") -> None:
        """Last words: persist the pane history before the kill so the
        console can still show a dead session's output. An empty capture
        (already-dead session, wedged server) writes nothing — end() runs
        again on dead sessions and must not truncate the snapshot the park
        wrote. A failed write must never keep the session alive."""
        if self.state_dir is None:
            return
        try:
            history = self.capture_history(target, issue, **({"repo": repo} if repo else {}))
            if not history:
                return
            path = (self.state_dir / "snapshots" /
                    f"task-{task_key(target, issue, repo)}.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(history)
        except OSError:
            return

    def end(self, target: str, issue: int, repo: str = "") -> None:
        if self.dry_run:
            print(f"[dry-run] end session {session_name(target, issue, repo)}")
            return
        self._snapshot(target, issue, repo)
        tab = self._tab(target, issue, repo)
        if tab is not None:
            tab.close()  # HUPs the shell and the -it podman
        # Belt and braces: the container may outlive the HUP. The unqualified
        # `task-<T>-<N>` is removed only for a caller that owns it; the
        # pre-rekey `task-<issue>` is shared by every target, so only an
        # unqualified caller may remove it.
        names = [session_name(target, issue, repo)]
        if not repo:
            names += [session_name(target, issue), f"task-{issue}"]
        elif self._legacy_owned(target, issue, repo):
            names.append(session_name(target, issue))
        for name in dict.fromkeys(names):
            subprocess.run(["podman", "rm", "-f", name],
                           capture_output=True, timeout=60)
        self.forget_status(target, issue, repo)
