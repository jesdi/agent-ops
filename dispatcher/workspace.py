"""Per-task worktree provisioning. Worktrees are never auto-deleted —
crashed ones are preserved for autopsy."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from dispatcher import containers
from dispatcher.config import Target

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"


def _sh(args: list[str], cwd: str, timeout: int = 300) -> None:
    subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                   timeout=timeout, check=True)


def create_workspace(target: Target, issue: int, dry_run: bool = False) -> str:
    wt = str(Path(target.worktrees_path) / f"task-{issue}")
    branch = f"agent/task-{issue}"
    if dry_run:
        print(f"[dry-run] create worktree {wt} on {branch}")
        return wt

    _sh(["git", "fetch", "origin"], cwd=target.clone_path)
    _sh(["git", "worktree", "add", "-b", branch, wt, "origin/main"],
        cwd=target.clone_path)
    _sh(containers.setup_cmd(f"task-{issue}-setup", wt, target.setup_cmd),
        cwd=wt, timeout=1800)

    agent_dir = Path(wt) / ".agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "task.json").write_text(json.dumps({"issue": issue}))

    exclude = Path(target.clone_path) / ".git" / "worktrees" / f"task-{issue}" / "info"
    exclude.mkdir(parents=True, exist_ok=True)
    (exclude / "exclude").write_text(".agent/\n.claude/settings.local.json\n")

    hook_dst = agent_dir / "stop-hook.sh"
    shutil.copy(HOOKS_DIR / "stop-hook.sh", hook_dst)
    hook_dst.chmod(0o755)

    claude_dir = Path(wt) / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "settings.local.json").write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "hooks": [{"type": "command", "command": ".agent/stop-hook.sh"}]
            }]
        }
    }, indent=2))
    return wt
