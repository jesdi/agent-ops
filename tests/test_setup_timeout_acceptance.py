"""Issue #17: a setup container that outlives the provisioning timeout must
not be left running. Exercises the REAL create_workspace and the REAL
subprocess path; only `podman` is a fake on PATH that behaves like the real
client: `run` registers the named container and blocks, and killing the
client (what subprocess.run's timeout does) leaves the container registered,
exactly as SIGKILL is not proxied to a real container. `rm -f` / `kill` /
`stop` remove it. The registry is one file per live container name.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

import dispatcher.workspace as workspace
from tests.test_workspace import repository

FAKE_PODMAN = """#!/bin/sh
reg="$FAKE_PODMAN_REGISTRY"
cmd="$1"; shift
case "$cmd" in
  run)
    name=""
    while [ $# -gt 0 ]; do
      case "$1" in --name) name="$2"; shift;; esac
      shift
    done
    if [ -e "$reg/$name" ]; then
      echo "Error: the container name \\"$name\\" is already in use" >&2; exit 125
    fi
    : > "$reg/$name"
    exec sleep 3600 ;;
  rm|kill|stop)
    for a in "$@"; do case "$a" in -*) ;; *) rm -f "$reg/$a";; esac; done ;;
esac
exit 0
"""


@pytest.fixture
def fake_podman(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(podman.stat().st_mode | stat.S_IEXEC)
    registry = tmp_path / "containers"
    registry.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PODMAN_REGISTRY", str(registry))
    return registry


@pytest.mark.xfail(strict=True, reason="#17: the fix removes this marker")
def test_setup_timeout_removes_the_setup_container(tmp_path, monkeypatch, fake_podman):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    real_sh = workspace._sh

    def sh(args, cwd, timeout=300, log=None, env=None):
        if args[0] == "git":  # git is not under test: create the worktree dir
            if args[:3] == ["git", "worktree", "add"]:
                wt = Path(args[-2])
                wt.mkdir(parents=True, exist_ok=True)
                (wt / ".git").write_text(
                    f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")
            return
        # The 1800 s provisioning timeout, shrunk to keep the test fast.
        real_sh(args, cwd, timeout=1 if timeout == 1800 else timeout, log=log, env=env)

    monkeypatch.setattr(workspace, "_sh", sh)
    t = repository(tmp_path)

    with pytest.raises(subprocess.TimeoutExpired):
        workspace.create_workspace(t, 42)

    assert sorted(p.name for p in fake_podman.iterdir()) == [], \
        "the timed-out setup container must be stopped and its name freed"
