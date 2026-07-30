"""Per-task worktree provisioning and merged-task teardown. Worktrees of crashed/failed tasks are never auto-deleted — preserved for autopsy."""
from __future__ import annotations

import json
import os
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
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _worktree_registered_at(clone_path: str, branch: str) -> str | None:
    """Return the worktree path where `branch` is currently registered
    according to `git worktree list --porcelain`, or None if it isn't
    registered anywhere (or the probe couldn't run — tolerant like
    `_branch_exists`, since this only ever guards the `-f` add and must
    not itself become a false-positive quarantine reason). Used so `-f`
    only ever resolves the "registered but missing" case it exists for,
    never silently creates a second live checkout of a branch that's
    already checked out elsewhere (e.g. after an operator relocates a
    crashed worktree with `git worktree move`)."""
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=clone_path, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    path = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def _mark_provisioned(wt: str) -> None:
    """Write a positive completion marker right after `git worktree add`
    (either form) returns successfully, so a later reuse can tell "this
    directory is a complete checkout" apart from "this directory is
    wreckage left by an add killed mid-run" without relying solely on the
    deleted-tracked-file heuristic in _worktree_health_issue."""
    agent_dir = Path(wt) / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "provisioned").touch()


def _status_porcelain_probe(wt: str) -> subprocess.CompletedProcess | None:
    """Run `git status --porcelain` with a generous timeout, retrying once
    on a timeout or execution error before giving up. Returns None if the
    probe could not be run even after the retry — that "probe could not
    run" outcome must never be treated as "probe found a problem" (a large
    worktree on a cold or contended volume can legitimately be slow)."""
    for attempt in range(2):
        try:
            return subprocess.run(
                ["git", "-C", wt, "status", "--porcelain"],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            if attempt == 1:
                return None
    return None


def _worktree_health_issue(wt: str, branch: str) -> str | None:
    """Cheap check for whether an existing worktree directory is a
    complete, correctly-attached checkout rather than the wreckage of a
    `git worktree add` killed mid-run. No caller wraps this in a timeout;
    its own internal budget is up to ~270s (a 30s rev-parse plus a 120s
    status probe retried once). Reads nothing but git metadata and the
    working tree; it deletes
    nothing, though `git status` can rewrite the index stat cache. Returns
    None when the worktree looks healthy, or a short description of
    what's wrong otherwise."""
    if not (Path(wt) / ".git").exists():
        return "missing .git"
    try:
        head = subprocess.run(
            ["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"branch check failed to run: {exc}"
    if head.returncode != 0:
        return f"git rev-parse HEAD failed: {head.stderr.strip()}"
    if head.stdout.strip() != branch:
        return f"on branch {head.stdout.strip()!r}, expected {branch!r}"

    if (Path(wt) / ".agent" / "provisioned").exists():
        # Positive completion marker from a prior successful `git
        # worktree add`: skip the deleted-tracked-file heuristic below
        # entirely, so setup-step code that legitimately deletes and
        # regenerates a tracked file (a lockfile, a staged rename under
        # status.renames=false) can't get this worktree permanently
        # quarantined on retry. Worktrees provisioned before this marker
        # existed simply fall through to the status check below.
        return None

    status = _status_porcelain_probe(wt)
    if status is None:
        # Probe never ran (timeout/error, even after a retry) — benign,
        # like _branch_exists: we can't conclude corruption from a probe
        # that never executed.
        return None
    if status.returncode != 0:
        return f"git status --porcelain failed: {status.stderr.strip()}"
    for line in status.stdout.splitlines():
        if "D" in line[:2]:
            return f"deleted tracked file(s) present, e.g. {line.strip()!r}"
    return None


def _seed_claude_state(wt: str) -> None:
    """Merge-write claude-home/.claude.json so stage containers never stall
    on an interactive dialog nobody is attached to answer: complete
    onboarding once, and pre-trust this worktree (the folder-trust dialog
    is per-directory, and every task gets a fresh worktree path).
    .claude.json is machine state — only these keys are asserted, the rest
    is preserved; an unreadable file starts fresh rather than failing
    provisioning."""
    home = Path(containers._state_dir()) / "claude-home"
    home.mkdir(parents=True, exist_ok=True)
    p = home / ".claude.json"
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    data["hasCompletedOnboarding"] = True
    data.setdefault("projects", {}).setdefault(wt, {})[
        "hasTrustDialogAccepted"] = True
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


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
        # exists" — reuse what's there and go straight to setup, but only
        # after confirming it's a complete checkout and not the wreckage
        # of a `git worktree add` killed mid-run (e.g. by the 300s
        # timeout), which would otherwise get silently claimed and
        # produce a PR missing arbitrary tracked files.
        problem = _worktree_health_issue(wt, branch)
        if problem is not None:
            raise RuntimeError(
                f"worktree {wt} exists but failed its health check "
                f"({problem}); refusing to reuse it — this needs manual "
                f"inspection, not a silent retry")
        _mark_provisioned(wt)
    elif _branch_exists(target.clone_path, branch):
        # Worktree was removed (e.g. manual cleanup) but the branch
        # survived — attach a fresh worktree to it instead of trying to
        # (re)create the branch with -b, which would die on "already
        # exists". Use -f: if the directory was rm -rf'd but the worktree
        # is still registered, a plain `add` dies on "missing but already
        # registered worktree"; -f succeeds and deletes/prunes nothing.
        # But a single -f is also enough to add a worktree on a branch
        # that's already checked out somewhere ELSE (e.g. an operator
        # preserved a crashed tree via `git worktree move`) — guard
        # against that before touching -f: it must only ever resolve the
        # registered-but-missing case it exists for.
        other = _worktree_registered_at(target.clone_path, branch)
        if other is not None and os.path.realpath(other) != os.path.realpath(wt):
            raise RuntimeError(
                f"branch {branch!r} is already checked out at {other!r}; "
                f"refusing to `git worktree add -f` a second checkout at "
                f"{wt!r} — this needs manual inspection, not a silent -f")
        _sh(["git", "fetch", "origin"], cwd=target.clone_path)
        _sh(["git", "worktree", "add", "-f", wt, branch], cwd=target.clone_path)
        _mark_provisioned(wt)
    else:
        _sh(["git", "fetch", "origin"], cwd=target.clone_path)
        _sh(["git", "worktree", "add", "-b", branch, wt, "origin/main"],
            cwd=target.clone_path)
        _mark_provisioned(wt)
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
                # Anchor to $CLAUDE_PROJECT_DIR, not a bare relative path:
                # Claude fires the Stop hook with the session's current cwd,
                # which need not be the worktree root. A relative command
                # 404s from any subdir ("/bin/sh: .agent/stop-hook.sh: not
                # found"), the waiting ping never fires, and the task hangs
                # unparked forever. Claude exports CLAUDE_PROJECT_DIR (the
                # worktree root) into every hook's env for exactly this.
                "hooks": [{"type": "command",
                           "command": "$CLAUDE_PROJECT_DIR/.agent/stop-hook.sh"}]
            }]
        }
    }, indent=2))

    _seed_claude_state(wt)
    return wt


def remove_workspace(target: Target, wt: str, branch: str,
                     dry_run: bool = False) -> None:
    """Merged-task teardown — the ONE sanctioned worktree deletion (the
    module rule "worktrees are never auto-deleted" still holds for crashed
    and failed tasks, which keep theirs for autopsy). Best-effort at every
    step: the branch is merged, so nothing here is load-bearing, and a
    failure must not abort the done path."""
    if dry_run:
        print(f"[dry-run] remove worktree {wt} and local branch {branch}")
        return

    try:
        _sh(["git", "worktree", "remove", "--force", wt],
            cwd=target.clone_path)
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(wt, ignore_errors=True)
        try:
            _sh(["git", "worktree", "prune"], cwd=target.clone_path)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        _sh(["git", "branch", "-D", branch], cwd=target.clone_path)
    except (OSError, subprocess.SubprocessError):
        pass  # already gone, or never created locally
