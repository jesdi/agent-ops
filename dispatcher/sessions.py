"""tmux adapter. One tmux session per task (task-<N>); each stage is a
fresh interactive claude invocation inside it — artifacts, not context,
cross stage boundaries."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _tmux(args: list[str]) -> int:
    return subprocess.run(args, capture_output=True, timeout=30).returncode


def session_name(issue: int) -> str:
    return f"task-{issue}"


class Sessions:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def is_alive(self, issue: int) -> bool:
        return _tmux(["tmux", "has-session", "-t", session_name(issue)]) == 0

    def spawn_stage(self, issue: int, worktree: str, prompt: str, stage_name: str) -> None:
        name = session_name(issue)
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' in tmux {name} at {worktree}")
            return
        agent_dir = Path(worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"prompt-{stage_name}.md").write_text(prompt)
        if _tmux(["tmux", "has-session", "-t", name]) != 0:
            _tmux(["tmux", "new-session", "-d", "-s", name, "-c", worktree])
        cmd = (
            f'claude --permission-mode acceptEdits '
            f'"$(cat .agent/prompt-{stage_name}.md)"'
        )
        _tmux(["tmux", "send-keys", "-t", name, cmd, "Enter"])

    def end(self, issue: int) -> None:
        if self.dry_run:
            print(f"[dry-run] end tmux {session_name(issue)}")
            return
        _tmux(["tmux", "kill-session", "-t", session_name(issue)])
