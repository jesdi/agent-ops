"""tmux + podman adapter. One tmux session per task (task-<N>); each stage
is a fresh `podman run … claude` inside it. tmux is the persistence/attach/
injection layer; the container is the isolation layer — they die together
at park and are recreated together at resume (claude --continue reads
transcripts from the mounted claude-home, keyed by the worktree cwd, which
is mounted at the same path inside the container)."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def _tmux(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, timeout=30).returncode


def session_name(issue: int) -> str:
    return f"task-{issue}"


def _state_dir() -> str:
    return os.environ.get("AGENT_OPS_STATE_DIR",
                          str(Path.home() / "agent-ops-state"))


def podman_cmd(issue: int, worktree: str, memory: str, cpus: str,
               claude_args: str) -> str:
    image = os.environ.get("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    home = str(Path.home())
    return (
        f"podman run --rm -it --name {session_name(issue)} "
        f"--memory {memory} --cpus {cpus} "
        f"-v {worktree}:{worktree} -w {worktree} "
        f"-v {_state_dir()}/claude-home:/root/.claude "
        f"-v {home}/.config/gh:/root/.config/gh:ro "
        f"-v {home}/.gitconfig:/root/.gitconfig:ro "
        f"{image} claude --permission-mode acceptEdits {claude_args}"
    )


class Sessions:
    def __init__(self, dry_run: bool = False, memory: str = "2g",
                 cpus: str = "2"):
        self.dry_run = dry_run
        self.memory = memory
        self.cpus = cpus

    def is_alive(self, issue: int) -> bool:
        return _tmux(["tmux", "has-session", "-t", session_name(issue)]) == 0

    def _launch(self, issue: int, worktree: str, claude_args: str) -> None:
        name = session_name(issue)
        if _tmux(["tmux", "has-session", "-t", name]) != 0:
            _tmux(["tmux", "new-session", "-d", "-s", name, "-c", worktree])
        cmd = podman_cmd(issue, worktree, self.memory, self.cpus, claude_args)
        _tmux(["tmux", "send-keys", "-t", name, cmd, "Enter"])

    def spawn_stage(self, issue: int, worktree: str, prompt: str,
                    stage_name: str) -> None:
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' in tmux "
                  f"{session_name(issue)} at {worktree}")
            return
        agent_dir = Path(worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"prompt-{stage_name}.md").write_text(prompt)
        self._launch(issue, worktree, f'"$(cat .agent/prompt-{stage_name}.md)"')

    def resume(self, issue: int, worktree: str, message: str) -> None:
        if self.dry_run:
            print(f"[dry-run] resume {session_name(issue)} at {worktree}")
            return
        self._launch(issue, worktree, f"--continue {shlex.quote(message)}")

    def capture_tail(self, issue: int, lines: int = 25) -> str:
        if self.dry_run:
            return ""
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name(issue)],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return "\n".join(out.stdout.rstrip().splitlines()[-lines:])

    def end(self, issue: int) -> None:
        if self.dry_run:
            print(f"[dry-run] end tmux {session_name(issue)}")
            return
        _tmux(["tmux", "kill-session", "-t", session_name(issue)])
        subprocess.run(["podman", "rm", "-f", session_name(issue)],
                       capture_output=True, timeout=60)
