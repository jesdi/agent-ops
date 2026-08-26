"""tmux + podman adapter. One tmux session per task (task-<target>-<issue>);
each stage is a fresh `podman run … claude` inside it. tmux is the
persistence/attach/injection layer; the container is the isolation layer —
they die together at park and are recreated together at resume (claude
--continue reads transcripts from the mounted claude-home, keyed by the
worktree cwd, which is mounted at the same path inside the container)."""
from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from dispatcher import containers


def _tmux(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, timeout=30).returncode


def session_name(target: str, issue: int) -> str:
    return f"task-{target}-{issue}"


def podman_cmd(target: str, issue: int, worktree: str, memory: str, cpus: str,
               model: str, claude_args: str) -> str:
    return containers.session_cmd(session_name(target, issue), worktree, memory,
                                  cpus, model, claude_args)


class Sessions:
    def __init__(self, dry_run: bool = False, memory: str = "2g",
                 cpus: str = "2", state_dir: str | Path | None = None):
        self.dry_run = dry_run
        self.memory = memory
        self.cpus = cpus
        self.state_dir = Path(state_dir) if state_dir else None

    def _resolve(self, target: str, issue: int) -> str:
        """Adopt a pre-rename tmux session on first touch: a deploy mid-flight
        must not strand live sessions under the old name."""
        name, legacy = session_name(target, issue), f"task-{issue}"
        if (_tmux(["tmux", "has-session", "-t", name]) != 0
                and _tmux(["tmux", "has-session", "-t", legacy]) == 0):
            _tmux(["tmux", "rename-session", "-t", legacy, name])
        return name

    def is_alive(self, target: str, issue: int) -> bool:
        name = self._resolve(target, issue)
        if _tmux(["tmux", "has-session", "-t", name]) == 0:
            return True
        # Adoption above renames on success, but a caller must still read
        # "alive" true if the legacy session exists and, for any reason
        # (e.g. a racing rename), wasn't adopted on this call.
        return _tmux(["tmux", "has-session", "-t", f"task-{issue}"]) == 0

    def _launch(self, target: str, issue: int, worktree: str, model: str,
                claude_args: str) -> None:
        name = self._resolve(target, issue)
        if _tmux(["tmux", "has-session", "-t", name]) != 0:
            _tmux(["tmux", "new-session", "-d", "-s", name, "-c", worktree])
        cmd = podman_cmd(target, issue, worktree, self.memory, self.cpus, model,
                         claude_args)
        _tmux(["tmux", "send-keys", "-t", name, cmd, "Enter"])

    def spawn_stage(self, target: str, issue: int, worktree: str, prompt: str,
                    stage_name: str, model: str) -> None:
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' on {model} in tmux "
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
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", name],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return "\n".join(out.stdout.rstrip().splitlines()[-lines:])

    def capture_history(self, target: str, issue: int, lines: int = 2000) -> str:
        """Console-owned pane history. Unlike capture_tail (visible pane
        only — the dispatcher's stall/login classification input), this
        pulls scrollback via -S so the web console can render true history.
        Degrades to '' on tmux failure exactly as capture_tail does, so a
        wedged tmux server never 500s the history view."""
        if self.dry_run:
            return ""
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", name],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return out.stdout.rstrip()

    def idle_seconds(self, target: str, issue: int) -> float | None:
        """Seconds since the task's tmux window last changed. Claude Code's
        status line redraws every second while working, so a live session
        reads near zero; only a truly static screen accumulates idle time.
        None = unknown (dry-run, tmux error) — callers must not treat it
        as stalled."""
        if self.dry_run:
            return None
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name,
             "#{window_activity}"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        try:
            return max(0.0, time.time() - int(out.stdout.strip()))
        except ValueError:
            return None

    def send_text(self, target: str, issue: int, text: str) -> None:
        """Type text into the live pane as-is (login-code injection). -l
        stops tmux interpreting the text as key names; Enter goes separately
        because -l would type the word 'Enter' literally."""
        if self.dry_run:
            print(f"[dry-run] send text to {session_name(target, issue)}")
            return
        name = self._resolve(target, issue)
        _tmux(["tmux", "send-keys", "-t", name, "-l", text])
        _tmux(["tmux", "send-keys", "-t", name, "Enter"])

    def _snapshot(self, target: str, issue: int) -> None:
        """Last words: persist the pane scrollback before the kill so the
        console can still show a dead session's output. An empty capture
        (already-dead session, wedged tmux) writes nothing — end() runs
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
        except (OSError, subprocess.SubprocessError):
            return

    def end(self, target: str, issue: int) -> None:
        if self.dry_run:
            print(f"[dry-run] end tmux {session_name(target, issue)}")
            return
        self._snapshot(target, issue)
        name = self._resolve(target, issue)
        _tmux(["tmux", "kill-session", "-t", name])
        subprocess.run(["podman", "rm", "-f", name],
                       capture_output=True, timeout=60)
