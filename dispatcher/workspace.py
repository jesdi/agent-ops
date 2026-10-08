"""Per-task worktree provisioning and merged-task teardown. Worktrees of crashed/failed tasks are never auto-deleted — preserved for autopsy."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from contextlib import contextmanager, suppress
from pathlib import Path

from dispatcher import containers
from dispatcher.config import Target

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"

# A task worktree, the clone and the runtime homes are mounted into the
# session's container, so a session can plant a symlink at any path the
# dispatcher writes there, or swap a directory for one. Every dispatcher write
# into such a place goes through the three functions below: the directory is
# opened once, component by component, without following a symlink, and the
# file is created and renamed relative to that descriptor. A rename replaces a
# symlink at the destination instead of following it, and a directory swapped
# after the open cannot redirect the write.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def _real_dir(root: str | Path, *parts: str):
    """A descriptor of <root>/<parts...>, each part a real directory, never
    a symlink. Only the last part is made when missing: the ones above it
    must be there. Raises OSError otherwise."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part == parts[-1]:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=fd)
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise OSError(exc.errno, f"{Path(root, *parts)}: {part!r} is not a "
                              f"real directory (a symlink?); nothing written") from exc
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _plain_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"not a plain file name: {name!r}")
    return name


def write_worktree_file(root: str | Path, subdir: str, name: str,
                        data: str | bytes, mode: int | None = 0o644) -> None:
    """Write <root>/<subdir>/<name> whole (temp file, then rename), never
    through a symlink. `subdir` may hold several components. A failed write
    leaves no temp file behind. `mode` None keeps the mode of the regular
    file that is replaced, and makes a new file 0600."""
    name = _plain_name(name)
    raw = data.encode() if isinstance(data, str) else data
    tmp = f".{name}.{secrets.token_hex(6)}.tmp"
    with _real_dir(root, *Path(subdir).parts) as dfd:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                os.fchmod(fh.fileno(), _kept_mode(dfd, name) if mode is None else mode)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise


def _kept_mode(dfd: int, name: str) -> int:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return 0o600
    return stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else 0o600


def append_worktree_file(root: str | Path, subdir: str, name: str, text: str) -> None:
    """Append to <root>/<subdir>/<name>: only to a regular file with one
    name (no symlink, no hard link to a file elsewhere)."""
    with _real_dir(root, *Path(subdir).parts) as dfd:
        fd = os.open(_plain_name(name), os.O_WRONLY | os.O_APPEND | os.O_CREAT
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                     0o644, dir_fd=dfd)
        with os.fdopen(fd, "a") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError(f"{Path(root, subdir, name)} is not a plain file; "
                              f"nothing appended")
            fh.write(text)


def read_worktree_file(root: str | Path, subdir: str, name: str,
                       max_bytes: int = 1024 * 1024) -> bytes | None:
    """The bytes of the regular file <root>/<subdir>/<name>, read without
    following a symlink at any component; None when there is no such file."""
    try:
        with _real_dir(root, *Path(subdir).parts) as dfd:
            fd = os.open(_plain_name(name), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=dfd)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    return None
                raw = fh.read(max_bytes + 1)
    except OSError:
        return None
    return raw if len(raw) <= max_bytes else None


def _write_log(log: Path, text: str) -> None:
    write_worktree_file(log.parent.parent, log.parent.name, log.name, text)


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
            _write_log(log, _decode(exc.stdout) + _decode(exc.stderr))
        raise
    if log is not None:
        _write_log(log, proc.stdout + proc.stderr)
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
    write_worktree_file(wt, ".agent", "provisioned", "")


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
    state_dir = Path(containers._state_dir())
    state_dir.mkdir(parents=True, exist_ok=True)
    data = _json_object(read_worktree_file(state_dir, "claude-home", ".claude.json"))
    data["hasCompletedOnboarding"] = True
    projects = data.get("projects")
    data["projects"] = projects = projects if isinstance(projects, dict) else {}
    trust = projects.get(wt)
    projects[wt] = trust = trust if isinstance(trust, dict) else {}
    trust["hasTrustDialogAccepted"] = True
    write_worktree_file(state_dir, "claude-home", ".claude.json",
                        json.dumps(data, indent=2) + "\n", mode=None)


def _json_object(raw: bytes | None) -> dict:
    """The JSON object in `raw`; {} for a missing, unparseable or other file."""
    try:
        data = json.loads(raw) if raw is not None else None
    except (ValueError, RecursionError):
        data = None
    return data if isinstance(data, dict) else {}


def install_stop_hook(wt: str) -> None:
    """Copy the Stop hook script into the worktree and point
    .claude/settings.local.json's hooks.Stop at it. Runs at provisioning and
    on every launch/resume, so a stale or deleted hook heals. Only hooks.Stop
    is asserted; other settings keys survive, and a missing, unparseable or
    non-object file is replaced."""
    # 0755: the box's checkout of the source may lack +x.
    write_worktree_file(wt, ".agent", "stop-hook.sh",
                        (HOOKS_DIR / "stop-hook.sh").read_bytes(), mode=0o755)
    settings = _json_object(read_worktree_file(wt, ".claude", "settings.local.json"))
    hooks = settings.get("hooks")
    settings["hooks"] = hooks = hooks if isinstance(hooks, dict) else {}
    # Anchor to $CLAUDE_PROJECT_DIR, not a bare relative path: Claude fires
    # the Stop hook with the session's current cwd, which need not be the
    # worktree root. A relative command 404s from any subdir, the waiting
    # ping never fires, and the task hangs unparked forever.
    hooks["Stop"] = [{"hooks": [{
        "type": "command",
        "command": "$CLAUDE_PROJECT_DIR/.agent/stop-hook.sh"}]}]
    # Whole or not at all: a live session may read it mid-resume.
    write_worktree_file(wt, ".claude", "settings.local.json",
                        json.dumps(settings, indent=2) + "\n")


LOCAL_STATE = (".agent/", ".claude/settings.local.json")


def exclude_local_state(clone_path: str) -> None:
    """Keep the dispatcher's files in a task worktree out of `git status`,
    so a session's `git add -A` never commits them. git reads info/exclude
    from the common directory only, never from a worktree's own, so the
    lines go in the clone's. Lines already there are kept, none is added twice.

    `clone_path` is the target's configured clone, never a path taken from a
    task worktree or from git: a worktree's `.git` file and the clone's git
    metadata are session-writable, and would name any directory on the host.
    The file is the fixed <clone_path>/.git/info/exclude, and `.git` and
    `info` must be real directories there (write_worktree_file). Anything
    else raises OSError."""
    raw = read_worktree_file(clone_path, ".git/info", "exclude")
    text = raw.decode(errors="replace") if raw is not None else ""
    missing = [line for line in LOCAL_STATE if line not in text.splitlines()]
    if missing:
        if text and not text.endswith("\n"):
            text += "\n"
        write_worktree_file(clone_path, ".git/info", "exclude",
                            text + "\n".join(missing) + "\n")


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
    if target.setup_cmd:
        _sh(containers.setup_cmd(f"task-{target.name}-{issue}-setup", wt,
                                 target.setup_cmd, target.clone_path),
            cwd=wt, timeout=1800, log=Path(wt) / ".agent" / "setup.log")

    write_worktree_file(wt, ".agent", "task.json", json.dumps(
        {"issue": issue, "target": target.name, "branch": branch}))

    exclude_local_state(target.clone_path)

    install_stop_hook(wt)

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
