"""create_workspace(repository, issue, base) against a real git origin and clone.

Real git on purpose: what these tests pin is git's own behavior (fetch --prune,
ls-remote exit codes, refspecs, ancestry, fast-forward), which a faked `_sh`
would only restate. Both repos live in tmp_path, origin is a local path (no
network), and conftest keeps the developer's git config out."""
import subprocess
from pathlib import Path

import pytest

import dispatcher.workspace as workspace
from dispatcher.config import Board, Project, Repository


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, name):
    (Path(repo) / name).write_text(name)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", name)


def push_branch(origin, name, commits=3):
    git(origin, "checkout", "-b", name, "main")
    for i in range(commits):
        commit(origin, f"{name.replace('/', '-')}-{i}")
    git(origin, "checkout", "main")


@pytest.fixture
def env(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    commit(origin, "base")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    t = Repository(
        name="shop", repo="jesdi/shop", github_id=1, clone_path=str(clone),
        worktrees_path=str(tmp_path / "clone.worktrees"),
        project=Project(
            name="shop", rank_cmd="rank",
            board=Board(
                number=1, owner="jesdi", status_field_id="F",
                status_ready_option_id="R", status_in_progress_option_id="I")))
    return origin, clone, t


def tip(repo, ref):
    return git(repo, "rev-parse", ref)


def test_branch_starts_at_tip_of_integration_base(env):
    origin, clone, t = env
    push_branch(origin, "feat/new-cart")
    wt = workspace.create_workspace(t, 42, "feat/new-cart")
    head = tip(wt, "HEAD")
    assert head == tip(origin, "feat/new-cart")
    assert head != tip(origin, "main")
    assert tip(clone, "agent/task-42") == head


def test_base_main_starts_at_tip_of_main(env):
    origin, clone, t = env
    push_branch(origin, "feat/new-cart")
    wt = workspace.create_workspace(t, 42, "main")
    assert tip(wt, "HEAD") == tip(origin, "main")


def test_base_pushed_after_last_fetch_is_found(env):
    origin, clone, t = env
    git(clone, "fetch", "origin")
    push_branch(origin, "feat/new-cart")
    assert "origin/feat/new-cart" not in git(clone, "branch", "-r")
    wt = workspace.create_workspace(t, 42, "feat/new-cart")
    assert tip(wt, "HEAD") == tip(origin, "feat/new-cart")


def test_missing_base_raises_and_leaves_nothing(env):
    origin, clone, t = env
    with pytest.raises(workspace.MissingBaseError) as exc:
        workspace.create_workspace(t, 42, "feat/new-cart")
    assert "feat/new-cart" in str(exc.value)
    assert not (Path(t.worktrees_path) / "task-42").exists()
    assert git(clone, "branch", "--list", "agent/task-42") == ""


def test_base_with_underscore_and_dot(env):
    origin, clone, t = env
    push_branch(origin, "feat/new-cart_v2.1")
    wt = workspace.create_workspace(t, 42, "feat/new-cart_v2.1")
    assert tip(wt, "HEAD") == tip(origin, "feat/new-cart_v2.1")
