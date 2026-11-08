"""The fixture dispatcher uses real Git without releasing other repositories."""
import os
from pathlib import Path
import subprocess
import sys

from fixture_git import executable

REAL = "/usr/bin/git"
WRAPPER = Path(__file__).with_name("fixture_git.py")


def git(*args):
    return subprocess.run([REAL, *map(str, args)], check=True, capture_output=True, text=True)


def test_fixture_main_push_and_branch_cleanup(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    repo, remote = fixture / "repo", fixture / "remote.git"
    git("init", repo)
    git("init", "--bare", remote)
    git("-C", repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
    git("-C", repo, "remote", "add", "origin", remote)
    git("-C", repo, "branch", "discard")
    guard = tmp_path / "guard"
    guard.write_text("#!/bin/sh\nexit 93\n")
    guard.chmod(0o755)
    env = dict(os.environ, FIXTURE_GIT_ROOT=str(fixture),
               FIXTURE_GIT_REAL=REAL, FIXTURE_GIT_GUARDED=str(guard))
    for args in (("push", "origin", "HEAD:main"), ("branch", "-D", "discard")):
        result = subprocess.run([sys.executable, str(WRAPPER), "-C", str(repo), *args],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert git("--git-dir", remote, "rev-parse", "main").stdout.strip()
    assert "discard" not in git("-C", repo, "branch").stdout


def test_outside_common_repository_keeps_guard(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    external = tmp_path / "external"
    git("init", external)
    git("-C", external, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "outside")
    linked = fixture / "linked"
    git("-C", external, "worktree", "add", linked)
    for target in (external, linked, tmp_path / "missing"):
        assert executable(["-C", str(target), "branch", "-D", "main"],
                          fixture, "/guarded/git", REAL) == "/guarded/git"
