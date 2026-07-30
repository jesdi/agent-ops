import json
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
    assert "task-42-setup" in setup_args
    assert f"{wt}:{wt}" in setup_args
    assert f"{t.clone_path}:{t.clone_path}" in setup_args
    assert "agent-ops-npm-cache:/root/.npm" in setup_args
    assert setup_args[-1] == "scripts/setup-worktree.sh"
    assert setup_cwd == wt
    assert setup_timeout == 1800

    assert json.loads((Path(wt) / ".agent" / "task.json").read_text()) == {"issue": 42}
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
    assert json.loads((wt_path / ".agent" / "task.json").read_text()) == {"issue": 42}


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
    assert json.loads((wt_path / ".agent" / "task.json").read_text()) == {"issue": 42}


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


def test_remove_workspace_survives_already_gone(tmp_path: Path):
    t = target(tmp_path)
    # clone_path doesn't exist; nope/ doesn't exist; branch never created —
    # remove_workspace must swallow all errors and return normally.
    workspace.remove_workspace(t, str(tmp_path / "nope"), "agent/task-56")
