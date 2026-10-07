import json
from dataclasses import replace
from pathlib import Path

import dispatcher.workspace as workspace
from dispatcher.config import Target


def target(tmp_path: Path) -> Target:
    return Target(
        name="portfolio_eval", repo="jesdi/portfolio_eval",
        clone_path=str(tmp_path / "repo"),
        worktrees_path=str(tmp_path / "repo.worktrees"),
        rank_cmd="rank", setup_cmd="scripts/setup-worktree.sh",
        verify_cmd="make e2e-slot SLOT={slot}",
        project_number=1, project_owner="jesdi",
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="I",
    )


def test_create_workspace(tmp_path: Path, monkeypatch):
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append((args, cwd, timeout))
        if "worktree" in args:  # simulate git creating the dir
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt = workspace.create_workspace(t, 42)

    assert wt == str(tmp_path / "repo.worktrees" / "task-42")
    fetch = calls[0]
    assert fetch[:2] == (["git", "fetch", "origin"], t.clone_path)
    add = calls[1]
    assert add[0][:3] == ["git", "worktree", "add"]
    assert "-b" in add[0], "fresh creation must use -b, not the existing-branch route"
    assert "-f" not in add[0], "fresh creation must not use -f — that's the existing-branch route"
    assert "agent/task-42" in add[0] and add[1] == t.clone_path
    setup_args, setup_cwd, setup_timeout = calls[2]
    assert setup_args[:3] == ["podman", "run", "--rm"]
    assert "task-portfolio_eval-42-setup" in setup_args
    assert f"{wt}:{wt}" in setup_args
    assert f"{t.clone_path}:{t.clone_path}" in setup_args
    assert "agent-ops-npm-cache:/root/.npm" in setup_args
    assert setup_args[-1] == "scripts/setup-worktree.sh"
    assert setup_cwd == wt
    assert setup_timeout == 1800

    assert json.loads((Path(wt) / ".agent" / "task.json").read_text()) == {"issue": 42, "target": "portfolio_eval", "branch": "agent/task-42"}
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    assert hook.exists() and hook.stat().st_mode & 0o111
    settings = json.loads((Path(wt) / ".claude" / "settings.local.json").read_text())
    stop = settings["hooks"]["Stop"][0]["hooks"][0]
    assert stop["type"] == "command" and ".agent/stop-hook.sh" in stop["command"]
    # The command must be cwd-independent: Claude fires the Stop hook with
    # whatever cwd the session currently holds, which is not guaranteed to be
    # the worktree root. A bare relative path silently 404s from any subdir,
    # the "waiting" ping never fires, and the task hangs unparked. Anchor it
    # to $CLAUDE_PROJECT_DIR so it resolves from anywhere.
    assert stop["command"].startswith("$CLAUDE_PROJECT_DIR/"), stop["command"]


def test_create_workspace_skips_setup_when_no_setup_cmd(tmp_path: Path, monkeypatch):
    """Requirement 5 / ticket 06: an empty setup_cmd means no provisioning
    container runs at all — not a podman invocation with an empty command
    tail. create_workspace must still create the worktree and return its
    path."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)
        if "worktree" in args:  # simulate git creating the dir
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = replace(target(tmp_path), setup_cmd="")

    wt = workspace.create_workspace(t, 42)

    assert wt == str(tmp_path / "repo.worktrees" / "task-42")
    assert not any(a[0] == "podman" for a in calls), \
        "no setup container may run when setup_cmd is empty"


def _make_healthy_worktree(wt_path: Path, branch: str) -> None:
    """Build a real worktree checked out on `branch` at `wt_path`, attached
    to a throwaway base repo, so the Finding-1 health check (branch match,
    no deleted tracked files) passes for real rather than being mocked
    away — and so `.git` is a real "gitdir:" file the way an actual
    worktree produces it, not a plain repo directory."""
    import subprocess as sp
    base = wt_path.parent / "_base_repo_for_health_check"
    base.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "init", "-q", str(base)], check=True)
    sp.run(["git", "-C", str(base), "config", "user.email", "t@example.com"],
           check=True)
    sp.run(["git", "-C", str(base), "config", "user.name", "test"], check=True)
    (base / "README.md").write_text("hi\n")
    sp.run(["git", "-C", str(base), "add", "README.md"], check=True)
    sp.run(["git", "-c", "commit.gpgsign=false", "-C", str(base), "commit",
            "-q", "-m", "init"], check=True)
    sp.run(["git", "-C", str(base), "worktree", "add", "-q", "-b", branch,
            str(wt_path)], check=True)


def test_create_workspace_reuses_existing_worktree_dir(tmp_path: Path, monkeypatch):
    """The most common provisioning failure is the setup step, which runs
    AFTER `git worktree add` already succeeded. A retry must not re-run
    `git worktree add` (which would die on "already exists") — it should
    skip straight to the setup step. The reused directory is a real,
    healthy checkout here (see the unhealthy-reuse test below for the
    Finding-1 rejection path)."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt_path = Path(t.worktrees_path) / "task-42"
    _make_healthy_worktree(wt_path, "agent/task-42")

    wt = workspace.create_workspace(t, 42)

    assert wt == str(wt_path)
    assert not any(a[:3] == ["git", "worktree", "add"] for a in calls), \
        "worktree already exists; git worktree add must be skipped entirely"
    assert any(a[0] == "podman" for a in calls), "setup step must still run"
    assert json.loads((wt_path / ".agent" / "task.json").read_text()) == {"issue": 42, "target": "portfolio_eval", "branch": "agent/task-42"}


def test_create_workspace_raises_on_unhealthy_reused_worktree(tmp_path: Path, monkeypatch):
    """A worktree directory that exists but fails the health check (e.g. a
    `.git` file left behind by a `git worktree add` killed mid-run, well
    short of a real checkout) must not be silently reused — that's exactly
    how a half-checked-out tree gets claimed and turned into a PR that
    deletes half the repo. create_workspace must raise and must never
    issue `git worktree add` against it."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt_path = Path(t.worktrees_path) / "task-42"
    wt_path.mkdir(parents=True)
    (wt_path / ".git").write_text(
        f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    with pytest.raises(Exception, match="failed its health check"):
        workspace.create_workspace(t, 42)

    assert not any(a[:3] == ["git", "worktree", "add"] for a in calls), \
        "an unhealthy worktree must never be handed to git worktree add"
    assert not calls, "setup step must not run either"


def test_create_workspace_reuses_existing_branch(tmp_path: Path, monkeypatch):
    """Worktree removed but the branch left behind (e.g. manual cleanup):
    add the worktree onto the existing branch instead of trying to create
    it again with -b, which would die on "branch already exists"."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)
        if args[:3] == ["git", "worktree", "add"]:
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setattr(workspace, "_branch_exists", lambda clone_path, branch: True)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)

    wt = workspace.create_workspace(t, 42)

    add = next(a for a in calls if a[:3] == ["git", "worktree", "add"])
    assert "-b" not in add, "must not try to (re)create the branch"
    assert "-f" in add, "must use -f — a plain add dies on a registered-but-missing worktree"
    assert add[-2:] == [wt, "agent/task-42"], \
        "must add the worktree onto the existing branch"


def test_create_workspace_raises_when_branch_checked_out_elsewhere(tmp_path: Path, monkeypatch):
    """Finding 2: a single -f is enough for git to add a worktree on a
    branch that's already checked out somewhere else. Scenario: an
    operator preserves a crashed tree by relocating it (`git worktree move
    task-42 task-42.crashed`) — the target worktree path is now free but
    the branch is still live at the relocated path. `-f` must not be
    allowed to spin up a second checkout of that branch; the new session
    and the preserved autopsy tree would then fight over one branch ref."""
    import subprocess as sp

    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    clone = Path(t.clone_path)
    clone.mkdir(parents=True)
    sp.run(["git", "init", "-q", str(clone)], check=True)
    sp.run(["git", "-C", str(clone), "config", "user.email", "t@example.com"],
           check=True)
    sp.run(["git", "-C", str(clone), "config", "user.name", "test"], check=True)
    (clone / "README.md").write_text("hi\n")
    sp.run(["git", "-C", str(clone), "add", "README.md"], check=True)
    sp.run(["git", "-c", "commit.gpgsign=false", "-C", str(clone), "commit",
            "-q", "-m", "init"], check=True)
    crashed = clone.parent / "task-42.crashed"
    sp.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b",
            "agent/task-42", str(crashed)], check=True)
    # target worktree path (task-42) is never created — this is the
    # "worktree removed, branch survived" case that normally takes -f.

    with pytest.raises(Exception, match="is already checked out at"):
        workspace.create_workspace(t, 42)

    assert not any(a[:3] == ["git", "worktree", "add"] for a in calls), \
        "must not create a second checkout of a branch already registered elsewhere"


def test_create_workspace_reuses_worktree_marked_provisioned_despite_deleted_file(
        tmp_path: Path, monkeypatch):
    """Finding 4: the reuse path exists for the "worktree add succeeded,
    setup failed" retry, and setup runs arbitrary target-repo code inside
    the worktree. If that code deletes a tracked file (e.g. regenerating a
    lockfile) and then fails partway, every later retry would otherwise
    see a `D` line in `git status --porcelain` and raise forever. The
    `provisioned` marker — written right after `git worktree add` succeeds
    — lets a retry skip that heuristic entirely."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt_path = Path(t.worktrees_path) / "task-42"
    _make_healthy_worktree(wt_path, "agent/task-42")
    (wt_path / "README.md").unlink()  # setup-step deleted a tracked file
    marker_dir = wt_path / ".agent"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "provisioned").touch()

    wt = workspace.create_workspace(t, 42)

    assert wt == str(wt_path)
    assert not any(a[:3] == ["git", "worktree", "add"] for a in calls), \
        "worktree is marked provisioned; git worktree add must be skipped"
    assert any(a[0] == "podman" for a in calls), "setup step must still run"
    assert json.loads((wt_path / ".agent" / "task.json").read_text()) == {"issue": 42, "target": "portfolio_eval", "branch": "agent/task-42"}


def test_create_workspace_raises_on_deleted_file_without_marker(
        tmp_path: Path, monkeypatch):
    """The flip side of the marker test above: a worktree that predates
    the `provisioned` marker (or never got one, e.g. killed mid-checkout)
    must still fall back to the deleted-tracked-file check and raise —
    the marker is an escape hatch for the completed-checkout case, not a
    blanket bypass."""
    calls = []

    def fake_sh(args, cwd, timeout=300, log=None):
        calls.append(args)

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt_path = Path(t.worktrees_path) / "task-42"
    _make_healthy_worktree(wt_path, "agent/task-42")
    (wt_path / "README.md").unlink()  # no marker written for this one

    with pytest.raises(Exception):
        workspace.create_workspace(t, 42)

    assert not any(a[:3] == ["git", "worktree", "add"] for a in calls), \
        "an unhealthy worktree must never be handed to git worktree add"
    assert not calls, "setup step must not run either"


def test_branch_exists_probe_tolerates_missing_clone(tmp_path: Path):
    """The probe must never raise: a missing clone_path (or any git error)
    reads as 'branch absent' so create_workspace falls through to today's
    -b creation path."""
    assert workspace._branch_exists(
        str(tmp_path / "no-such-clone"), "agent/task-42") is False


def test_dry_run_creates_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workspace, "_sh",
                        lambda a, cwd, timeout=300: (_ for _ in ()).throw(AssertionError))
    wt = workspace.create_workspace(target(tmp_path), 42, dry_run=True)
    assert not Path(wt).exists()


import subprocess

import pytest


def test_setup_output_written_to_setup_log(tmp_path: Path, monkeypatch):
    def fake_run(args, cwd=None, capture_output=True, text=True,
                 timeout=300, **kw):
        if args[:3] == ["git", "worktree", "add"]:
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")
        if args[0] == "podman":
            return subprocess.CompletedProcess(args, 1, stdout="out line\n",
                                               stderr="pipenv: no 3.13\n")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    t = target(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        workspace.create_workspace(t, 42)
    log = Path(t.worktrees_path) / "task-42" / ".agent" / "setup.log"
    assert log.read_text() == "out line\npipenv: no 3.13\n"


def test_setup_timeout_still_writes_partial_log(tmp_path: Path, monkeypatch):
    def fake_run(args, cwd=None, capture_output=True, text=True,
                 timeout=300, **kw):
        if args[:3] == ["git", "worktree", "add"]:
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")
        if args[0] == "podman":
            raise subprocess.TimeoutExpired(
                args, timeout, output=b"partial out\n", stderr=b"partial err\n")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(workspace.subprocess, "run", fake_run)
    t = target(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        workspace.create_workspace(t, 42)
    log = Path(t.worktrees_path) / "task-42" / ".agent" / "setup.log"
    assert log.read_text() == "partial out\npartial err\n"


def test_create_workspace_seeds_claude_trust(tmp_path: Path, monkeypatch):
    """Provisioning must pre-trust the worktree (and complete onboarding) in
    claude-home's .claude.json: stage containers run interactive `claude`
    with nobody attached, so a first-run wizard or folder-trust dialog
    stalls the task forever (task #192 sat on the theme picker for 1.5h)."""
    def fake_sh(args, cwd, timeout=300, log=None):
        if "worktree" in args:
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    # Pre-existing machine state must be merged, never clobbered.
    home = state / "claude-home"
    home.mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps(
        {"machineID": "m1", "projects": {"/old": {"lastCost": 1}}}))

    wt = workspace.create_workspace(target(tmp_path), 42)

    data = json.loads((home / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True
    assert data["projects"][wt]["hasTrustDialogAccepted"] is True
    assert data["machineID"] == "m1"
    assert data["projects"]["/old"] == {"lastCost": 1}


def test_seed_claude_state_survives_missing_or_corrupt_file(tmp_path: Path,
                                                           monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    workspace._seed_claude_state("/wt/a")           # no claude-home at all
    p = state / "claude-home" / ".claude.json"
    assert json.loads(p.read_text())["projects"]["/wt/a"][
        "hasTrustDialogAccepted"] is True
    p.write_text("{corrupt")
    workspace._seed_claude_state("/wt/b")           # unreadable → start fresh
    assert json.loads(p.read_text())["projects"]["/wt/b"][
        "hasTrustDialogAccepted"] is True


# ---------------------------------------------------------------------------
# remove_workspace tests
# ---------------------------------------------------------------------------

def _make_real_worktree_in_clone(tmp_path: Path, issue_num: int):
    """Build a real git repo at target.clone_path and add a worktree for
    agent/task-{issue_num}, returning (target, wt_path_str).  Matches the
    pattern of _make_healthy_worktree but roots the base repo at
    target.clone_path so git worktree commands run there correctly."""
    import subprocess as sp
    t = target(tmp_path)
    clone = Path(t.clone_path)
    clone.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "init", "-q", str(clone)], check=True)
    sp.run(["git", "-C", str(clone), "config", "user.email", "t@example.com"],
           check=True)
    sp.run(["git", "-C", str(clone), "config", "user.name", "test"], check=True)
    (clone / "README.md").write_text("hi\n")
    sp.run(["git", "-C", str(clone), "add", "README.md"], check=True)
    sp.run(["git", "-c", "commit.gpgsign=false", "-C", str(clone), "commit",
            "-q", "-m", "init"], check=True)
    wt_path = Path(t.worktrees_path) / f"task-{issue_num}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"agent/task-{issue_num}"
    sp.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b", branch,
            str(wt_path)], check=True)
    return t, str(wt_path)


def test_remove_workspace_removes_worktree_and_local_branch(tmp_path: Path):
    t, wt = _make_real_worktree_in_clone(tmp_path, 55)
    assert Path(wt).exists()
    workspace.remove_workspace(t, wt, "agent/task-55")
    assert not Path(wt).exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "agent/task-55"],
        cwd=t.clone_path, capture_output=True, text=True).stdout
    assert branches.strip() == ""


def test_remove_workspace_dry_run_deletes_nothing(tmp_path: Path):
    """--dry-run must not destroy anything: the real git commands here are
    the only side effect of a merged-task teardown that isn't already
    no-opped by GitHubClient/Sessions under dry_run."""
    t, wt = _make_real_worktree_in_clone(tmp_path, 57)
    workspace.remove_workspace(t, wt, "agent/task-57", dry_run=True)
    assert Path(wt).exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "agent/task-57"],
        cwd=t.clone_path, capture_output=True, text=True).stdout
    assert branches.strip() != ""


def test_remove_workspace_survives_already_gone(tmp_path: Path):
    t = target(tmp_path)
    # clone_path doesn't exist; nope/ doesn't exist; branch never created —
    # remove_workspace must swallow all errors and return normally.
    workspace.remove_workspace(t, str(tmp_path / "nope"), "agent/task-56")


def test_install_stop_hook_never_leaves_half_written_settings(tmp_path, monkeypatch):
    """A live session may read settings.local.json while a resume rewrites
    it: the write lands whole (temp file, then rename) or not at all."""
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{"keep": 1}')

    real_replace = workspace.os.replace

    def no_settings_rename(src, dst, **kw):
        if dst == "settings.local.json":
            raise OSError("interrupted")
        return real_replace(src, dst, **kw)
    monkeypatch.setattr(workspace.os, "replace", no_settings_rename)
    with pytest.raises(OSError):
        workspace.install_stop_hook(str(tmp_path))
    assert settings.read_text() == '{"keep": 1}'
    assert [p.name for p in settings.parent.iterdir()] == ["settings.local.json"]


# ---------------------------------------------------------------------------
# .agent/ is local state: git must not offer it for a commit (real git)
# ---------------------------------------------------------------------------

def _clone_with_origin(tmp_path: Path, monkeypatch) -> Target:
    """A real clone at target.clone_path whose origin has a `main`."""
    import subprocess as sp
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    origin = tmp_path / "origin"
    sp.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    (origin / "README.md").write_text("hi\n")
    sp.run(["git", "-C", str(origin), "add", "README.md"], check=True)
    sp.run(["git", "-C", str(origin), "-c", "user.email=t@example.com",
            "-c", "user.name=test", "-c", "commit.gpgsign=false", "commit",
            "-q", "-m", "init"], check=True)
    t = replace(target(tmp_path), setup_cmd="")
    sp.run(["git", "clone", "-q", str(origin), t.clone_path], check=True)
    return t


def _untracked(wt: str) -> str:
    import subprocess as sp
    return sp.run(["git", "-C", wt, "status", "--porcelain", "--untracked-files=all"],
                  check=True, capture_output=True, text=True).stdout


def test_agent_dir_is_ignored_by_git_in_a_task_worktree(tmp_path: Path, monkeypatch):
    """A session's `git add -A` must never stage the ledger, the tickets or
    the plan summary, even when the target's own .gitignore lacks `.agent/`."""
    t = _clone_with_origin(tmp_path, monkeypatch)

    wt = workspace.create_workspace(t, 42)
    (Path(wt) / ".agent" / "ledger.md").write_text("01 merged\n")

    assert _untracked(wt) == ""
    assert (Path(wt) / ".claude" / "settings.local.json").exists()   # ignored too


def test_exclude_lines_are_added_once_and_keep_the_operators_own(tmp_path: Path,
                                                                 monkeypatch):
    t = _clone_with_origin(tmp_path, monkeypatch)
    exclude = Path(t.clone_path) / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("# mine\n*.swp")          # no trailing newline

    for issue in (42, 43):
        workspace.create_workspace(t, issue)

    lines = exclude.read_text().splitlines()
    assert lines[:2] == ["# mine", "*.swp"]
    assert sorted(lines[2:]) == [".agent/", ".claude/settings.local.json"]
    assert exclude.read_text().endswith("\n")


# --- writes into a session-controlled directory never leave it ---------------

def _outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    (out / "victim").write_text("untouched")
    return out


def test_write_replaces_a_planted_symlink_instead_of_following_it(tmp_path):
    out = _outside(tmp_path)
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "stage.json").symlink_to(out / "victim")
    workspace.write_worktree_file(str(wt), ".agent", "stage.json", "new")
    assert (out / "victim").read_text() == "untouched"
    written = wt / ".agent" / "stage.json"
    assert not written.is_symlink() and written.read_text() == "new"
    assert sorted(p.name for p in (wt / ".agent").iterdir()) == ["stage.json"]


def test_write_refuses_a_directory_that_is_a_symlink(tmp_path):
    import pytest
    out = _outside(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".agent").symlink_to(out, target_is_directory=True)
    with pytest.raises(OSError, match="not a real directory"):
        workspace.write_worktree_file(str(wt), ".agent", "stage.json", "new")
    with pytest.raises(OSError):
        workspace.append_worktree_file(str(wt), ".agent", "models.log", "line\n")
    assert sorted(p.name for p in out.iterdir()) == ["victim"]


def test_write_takes_no_path_in_the_name_and_leaves_no_temp_file(tmp_path):
    import pytest
    wt = tmp_path / "wt"
    (wt / ".agent" / "stage.json").mkdir(parents=True)   # a directory in the way
    with pytest.raises(ValueError):
        workspace.write_worktree_file(str(wt), ".agent", "../x", "new")
    with pytest.raises(OSError):
        workspace.write_worktree_file(str(wt), ".agent", "stage.json", "new")
    assert sorted(p.name for p in (wt / ".agent").iterdir()) == ["stage.json"]
    assert not (wt / "x").exists()


def test_append_refuses_a_symlink_and_a_hard_link(tmp_path):
    import os
    import pytest
    out = _outside(tmp_path)
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    workspace.append_worktree_file(str(wt), ".agent", "models.log", "a\n")
    workspace.append_worktree_file(str(wt), ".agent", "models.log", "b\n")
    assert (wt / ".agent" / "models.log").read_text() == "a\nb\n"
    for plant in (lambda p: p.symlink_to(out / "victim"),
                  lambda p: os.link(out / "victim", p)):
        (wt / ".agent" / "models.log").unlink()
        plant(wt / ".agent" / "models.log")
        with pytest.raises(OSError):
            workspace.append_worktree_file(str(wt), ".agent", "models.log", "c\n")
        assert (out / "victim").read_text() == "untouched"


def test_stop_hook_install_neither_reads_nor_writes_through_symlinks(tmp_path):
    out = _outside(tmp_path)
    (out / "secret.json").write_text(json.dumps({"token": "host-secret"}))
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".claude").mkdir()
    (wt / ".agent" / "stop-hook.sh").symlink_to(out / "victim")
    (wt / ".claude" / "settings.local.json").symlink_to(out / "secret.json")
    (wt / ".claude" / "settings.local.json.tmp").symlink_to(out / "victim")
    workspace.install_stop_hook(str(wt))
    assert (out / "victim").read_text() == "untouched"
    assert json.loads((out / "secret.json").read_text()) == {"token": "host-secret"}
    settings = wt / ".claude" / "settings.local.json"
    assert not settings.is_symlink() and "host-secret" not in settings.read_text()
    hook = wt / ".agent" / "stop-hook.sh"
    assert not hook.is_symlink() and hook.stat().st_mode & 0o111


# --- the exclude lines: any git layout, healed on every launch ---------------

def test_exclude_lines_go_to_the_common_git_dir_when_dot_git_is_a_file(tmp_path, monkeypatch):
    import subprocess as sp
    clone, gitdir = tmp_path / "clone", tmp_path / "elsewhere.git"
    sp.run(["git", "init", "-q", "--separate-git-dir", str(gitdir), str(clone)], check=True)
    assert (clone / ".git").is_file()
    workspace.exclude_local_state(str(clone))
    assert (gitdir / "info" / "exclude").read_text().splitlines()[-2:] == [
        ".agent/", ".claude/settings.local.json"]


def test_exclude_lines_heal_from_a_task_worktree_that_existed_before(tmp_path, monkeypatch):
    """A worktree made before the exclude lines existed gets them at its next
    session start: the launch path calls this with the worktree."""
    t = _clone_with_origin(tmp_path, monkeypatch)
    wt = workspace.create_workspace(t, 42)
    exclude = Path(t.clone_path) / ".git" / "info" / "exclude"
    exclude.write_text("# mine\n")
    (Path(wt) / ".agent" / "ledger.md").write_text("01 merged\n")
    assert _untracked(wt) != ""
    workspace.exclude_local_state(wt)
    assert _untracked(wt) == "" and exclude.read_text().startswith("# mine\n")


def test_exclude_write_is_whole_and_follows_no_symlink(tmp_path, monkeypatch):
    t = _clone_with_origin(tmp_path, monkeypatch)
    out = _outside(tmp_path)
    exclude = Path(t.clone_path) / ".git" / "info" / "exclude"
    exclude.unlink(missing_ok=True)
    exclude.symlink_to(out / "victim")
    workspace.exclude_local_state(t.clone_path)
    assert (out / "victim").read_text() == "untouched"
    assert not exclude.is_symlink() and ".agent/" in exclude.read_text().splitlines()
    assert [p.name for p in exclude.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_exclude_on_a_directory_that_is_no_repository_does_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    workspace.exclude_local_state(str(plain))        # must not raise
    workspace.exclude_local_state(str(tmp_path / "missing"))
    assert list(plain.iterdir()) == []
