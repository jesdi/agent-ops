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


def _sh(args: list[str], cwd: str, timeout: int = 300,
        log: Path | None = None) -> None:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if log is not None:
            def _decode(v):
                if v is None:
                    return ""
                if isinstance(v, bytes):
                    return v.decode(errors="replace")
                return v
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(_decode(exc.stdout) + _decode(exc.stderr))
        raise
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(proc.stdout + proc.stderr)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args,
                                            proc.stdout, proc.stderr)


def _branch_exists(clone_path: str, branch: str) -> bool:
    """Tolerant probe: a missing clone_path or any git error reads as
    'branch absent' so create_workspace falls through to today's -b
    creation path — the probe itself must never raise."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=clone_path, capture_output=True, text=True, timeout=30)
    except OSError:
        return False
    return proc.returncode == 0


def create_workspace(target: Target, issue: int, dry_run: bool = False) -> str:
    wt = str(Path(target.worktrees_path) / f"task-{issue}")
    branch = f"agent/task-{issue}"
    if dry_run:
        print(f"[dry-run] create worktree {wt} on {branch}")
        return wt

    if Path(wt).exists():
        # Retry after a partial provisioning failure: the worktree (and
        # branch) were already created and only the setup step below
        # failed. Re-running `git worktree add` here would die on "already
        # exists" — reuse what's there and go straight to setup.
        pass
    elif _branch_exists(target.clone_path, branch):
        # Worktree was removed (e.g. manual cleanup) but the branch
        # survived — attach a fresh worktree to it instead of trying to
        # (re)create the branch with -b, which would die on "already
        # exists".
        _sh(["git", "fetch", "origin"], cwd=target.clone_path)
        _sh(["git", "worktree", "add", wt, branch], cwd=target.clone_path)
    else:
        _sh(["git", "fetch", "origin"], cwd=target.clone_path)
        _sh(["git", "worktree", "add", "-b", branch, wt, "origin/main"],
            cwd=target.clone_path)
    _sh(containers.setup_cmd(f"task-{issue}-setup", wt, target.setup_cmd),
        cwd=wt, timeout=1800, log=Path(wt) / ".agent" / "setup.log")

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
