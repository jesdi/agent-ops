"""Session layer: one session per task (task-<target>-<issue>), each stage
a fresh `podman run … claude` inside it. The session is a herdr tab —
`Tab(session_name(target, issue))` in the workspace labelled `<target>` —
whose root pane hosts the `podman run … claude`; the container is the
isolation layer. Tab and container die together at park and are recreated
together at resume (claude --continue reads transcripts from the mounted
claude-home, keyed by the worktree cwd, which is mounted at the same path
inside the container).

Tabs are resolved by label on every call and never persisted, so a herdr
restart or renumbering cannot strand a task. Every herdr failure degrades
instead of raising: is_alive → False, captures → "", idle_seconds → None,
mutations best-effort."""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

from dispatcher import containers, herdr


def session_name(target: str, issue: int) -> str:
    return f"task-{target}-{issue}"


def podman_cmd(target: str, issue: int, worktree: str, memory: str, cpus: str,
               model: str, claude_args: str) -> str:
    return containers.session_cmd(session_name(target, issue), worktree, memory,
                                  cpus, model, claude_args)


class Sessions:
    """The dispatcher's and the console's one session interface. Every
    operation resolves the tab by label; dry-run short-circuits each
    mutation and each capture before the server is asked."""

    def __init__(self, dry_run: bool = False, memory: str = "2g",
                 cpus: str = "2", state_dir: str | Path | None = None):
        self.dry_run = dry_run
        self.memory = memory
        self.cpus = cpus
        self.state_dir = Path(state_dir) if state_dir else None

    def _tab(self, target: str, issue: int) -> herdr.Tab | None:
        return herdr.Tab.find(session_name(target, issue))

    def is_alive(self, target: str, issue: int) -> bool:
        """Alive means the tab exists AND its shell is busy — a claude that
        has exited back to the host shell reads dead (the crash path owns
        it), and a tab restored by a herdr server restart reads dead.
        `main._inject_login_code` still re-verifies the prompt with
        `capture_tail` before typing."""
        tab = self._tab(target, issue)
        return tab is not None and tab.alive

    def _launch(self, target: str, issue: int, worktree: str, model: str,
                claude_args: str) -> None:
        tab = herdr.Tab.ensure(
            target, session_name(target, issue), worktree,
            # herdr's hint for detecting an agent behind a wrapper (podman
            # here). Tab-level env so the shell inherits it into every launch
            # and it never appears in the typed command; a herdr concern, so
            # it lives here and never in containers.session_cmd — the
            # headless triage container shares that module and must NOT read
            # as an agent.
            env={"HERDR_AGENT": "claude"})
        if tab is None:
            return  # server down: the task reads dead, the crash path owns it
        tab.run(podman_cmd(target, issue, worktree, self.memory, self.cpus,
                           model, claude_args))

    def spawn_stage(self, target: str, issue: int, worktree: str, prompt: str,
                    stage_name: str, model: str) -> None:
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' on {model} in session "
                  f"{session_name(target, issue)} at {worktree}")
            return
        agent_dir = Path(worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"prompt-{stage_name}.md").write_text(prompt)
        self._launch(target, issue, worktree, model,
                     f'"$(cat .agent/prompt-{stage_name}.md)"')

    def resume(self, target: str, issue: int, worktree: str, message: str,
               model: str) -> None:
        if self.dry_run:
            print(f"[dry-run] resume {session_name(target, issue)} on {model} "
                  f"at {worktree}")
            return
        self._launch(target, issue, worktree, model,
                     f"--continue {shlex.quote(message)}")

    def capture_tail(self, target: str, issue: int, lines: int = 25) -> str:
        if self.dry_run:
            return ""
        tab = self._tab(target, issue)
        text = tab.read("visible", lines) if tab else None
        if text is None:
            return ""
        return "\n".join(text.rstrip().splitlines()[-lines:])

    def capture_history(self, target: str, issue: int, lines: int = 2000) -> str:
        """Console-owned pane history (true scrollback, unlike capture_tail —
        the dispatcher's stall/login classification input). recent-unwrapped
        joins soft wraps, so the console renders the lines claude drew.
        Degrades to '' so a wedged server never 500s the history view."""
        if self.dry_run:
            return ""
        tab = self._tab(target, issue)
        text = tab.read("recent-unwrapped", lines) if tab else None
        return "" if text is None else text.rstrip()

    def idle_seconds(self, target: str, issue: int) -> float | None:
        """Seconds the session has been static; None = unknown (dry-run,
        herdr error) — callers must not treat it as stalled. Computed from
        the agent lifecycle, not screen activity: `working` is never idle,
        however long it lasts; any other status (idle / blocked / done /
        unknown, or no agent at all — the pane back at the host shell)
        accumulates from the moment herdr last changed its mind. herdr
        exposes the transition counter but no timestamp, so the moment is
        remembered in a sidecar keyed by (seq, status)."""
        if self.dry_run:
            return None
        tab = self._tab(target, issue)
        state = tab.agent_state() if tab else None
        if state is None:
            return None
        status, seq = state
        if status == "working":
            return 0.0
        return self._since_change(target, issue, status, seq)

    def _sidecar(self, target: str, issue: int) -> Path | None:
        if self.state_dir is None:
            return None
        return (self.state_dir / "herdr-status"
                / f"{session_name(target, issue)}.json")

    def _since_change(self, target: str, issue: int, status: str,
                      seq: int) -> float | None:
        path = self._sidecar(target, issue)
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
                return max(0.0, now - float(stored["since"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"seq": seq, "status": status, "since": now}))
            return 0.0
        except (OSError, KeyError, TypeError, ValueError):
            return None  # unknown, never a stale number

    def forget_status(self, target: str, issue: int) -> None:
        path = self._sidecar(target, issue)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def send_text(self, target: str, issue: int, text: str) -> None:
        """Type text into the live pane as-is (login-code injection), then
        Enter as a separate key."""
        if self.dry_run:
            print(f"[dry-run] send text to {session_name(target, issue)}")
            return
        tab = self._tab(target, issue)
        if tab is None:
            return
        tab.send_text(text)
        tab.send_keys("enter")

    def _snapshot(self, target: str, issue: int) -> None:
        """Last words: persist the pane history before the kill so the
        console can still show a dead session's output. An empty capture
        (already-dead session, wedged server) writes nothing — end() runs
        again on dead sessions and must not truncate the snapshot the park
        wrote. A failed write must never keep the session alive."""
        if self.state_dir is None:
            return
        try:
            history = self.capture_history(target, issue)
            if not history:
                return
            path = (self.state_dir / "snapshots" /
                    f"{session_name(target, issue)}.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(history)
        except OSError:
            return

    def end(self, target: str, issue: int) -> None:
        if self.dry_run:
            print(f"[dry-run] end session {session_name(target, issue)}")
            return
        name = session_name(target, issue)
        self._snapshot(target, issue)
        tab = self._tab(target, issue)
        if tab is not None:
            tab.close()  # HUPs the shell and the -it podman
        # Belt and braces: the container may outlive the HUP.
        subprocess.run(["podman", "rm", "-f", name],
                       capture_output=True, timeout=60)
        # Pre-rekey containers are named task-<issue> and nothing renames them,
        # so a wedged one leaks unless end() reaches for that name too.
        subprocess.run(["podman", "rm", "-f", f"task-{issue}"],
                       capture_output=True, timeout=60)
        self.forget_status(target, issue)
