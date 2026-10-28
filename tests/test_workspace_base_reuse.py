"""A stale tracking ref and a reused task branch never stand in for the task's base."""
import shutil
from pathlib import Path

import pytest

import dispatcher.workspace as workspace
from tests.test_workspace_base_acceptance import env, git, push_branch, tip  # noqa: F401


def test_base_deleted_on_origin_after_a_fetch_is_missing(env):
    origin, clone, t = env
    push_branch(origin, "feat/new-cart")
    git(clone, "fetch", "origin")
    git(origin, "branch", "-D", "feat/new-cart")
    with pytest.raises(workspace.MissingBaseError):
        workspace.create_workspace(t, 42, "feat/new-cart")
    assert not (Path(t.worktrees_path) / "task-42").exists()


def test_reused_worktree_from_another_base_is_refused(env):
    origin, clone, t = env
    push_branch(origin, "feat/a")
    push_branch(origin, "feat/b")
    workspace.create_workspace(t, 42, "feat/a")   # setup "failed" after this
    with pytest.raises(RuntimeError, match="outside"):
        workspace.create_workspace(t, 42, "feat/b")


def test_surviving_branch_from_another_base_is_refused(env):
    origin, clone, t = env
    push_branch(origin, "feat/a")
    push_branch(origin, "feat/b")
    wt = workspace.create_workspace(t, 42, "feat/a")
    shutil.rmtree(wt)
    with pytest.raises(RuntimeError, match="outside"):
        workspace.create_workspace(t, 42, "feat/b")


def test_reused_worktree_on_the_same_base_is_reused(env):
    origin, clone, t = env
    push_branch(origin, "feat/a")
    wt = workspace.create_workspace(t, 42, "feat/a")
    assert workspace.create_workspace(t, 42, "feat/a") == wt
    assert tip(wt, "HEAD") == tip(origin, "feat/a")


def test_reused_worktree_from_an_ancestor_base_starts_at_the_new_base(env):
    # setup failed on a main checkout, then the issue's base line moved to feat/new-cart
    origin, clone, t = env
    wt = workspace.create_workspace(t, 42, "main")
    push_branch(origin, "feat/new-cart")
    assert workspace.create_workspace(t, 42, "feat/new-cart") == wt
    assert tip(wt, "HEAD") == tip(origin, "feat/new-cart")
    assert tip(clone, "agent/task-42") == tip(origin, "feat/new-cart")


def test_surviving_branch_from_an_ancestor_base_starts_at_the_new_base(env):
    origin, clone, t = env
    wt = workspace.create_workspace(t, 42, "main")
    shutil.rmtree(wt)
    push_branch(origin, "feat/new-cart")
    workspace.create_workspace(t, 42, "feat/new-cart")
    assert tip(wt, "HEAD") == tip(origin, "feat/new-cart")


def narrow_fetch(clone):
    git(clone, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")


def test_base_deleted_on_origin_outside_a_narrow_refspec_is_missing(env):
    origin, clone, t = env
    push_branch(origin, "feat/new-cart")
    git(clone, "fetch", "origin")
    narrow_fetch(clone)
    git(origin, "branch", "-D", "feat/new-cart")
    with pytest.raises(workspace.MissingBaseError):
        workspace.create_workspace(t, 42, "feat/new-cart")
    assert not (Path(t.worktrees_path) / "task-42").exists()


def test_base_outside_a_narrow_refspec_is_found_at_its_origin_tip(env):
    origin, clone, t = env
    narrow_fetch(clone)
    push_branch(origin, "feat/new-cart")
    wt = workspace.create_workspace(t, 42, "feat/new-cart")
    assert tip(wt, "HEAD") == tip(origin, "feat/new-cart")
