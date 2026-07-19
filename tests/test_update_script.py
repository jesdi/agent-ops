"""E2E tests for provision/update.sh in a sandboxed git + fake-systemctl rig."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import fcntl
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "provision" / "update.sh"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def commit_all(cwd, msg):
    git(cwd, "add", "-A")
    git(cwd, "commit", "-m", msg)


@pytest.fixture
def box(tmp_path):
    # "origin" — a normal repo we commit to, cloned as the box checkout.
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "pyproject.toml").write_text("[project]\nname = 'agent-ops'\n")
    (origin / "provision").mkdir()
    (origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v1\n")
    (origin / "provision" / "agent-ops-dispatcher.timer").write_text(
        "[Unit]\nDescription=timer v1\n")
    commit_all(origin, "init")

    repo = tmp_path / "agent-ops"
    subprocess.run(["git", "clone", str(origin), str(repo)],
                   check=True, capture_output=True)

    # Fake systemctl and fake venv pip, both appending to calls.log.
    calls = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sysctl = bin_dir / "systemctl"
    sysctl.write_text(f'#!/bin/sh\necho "systemctl $@" >> "{calls}"\n')
    sysctl.chmod(0o755)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    pip = venv_bin / "pip"
    pip.write_text(f'#!/bin/sh\necho "pip $@" >> "{calls}"\n')
    pip.chmod(0o755)

    state = tmp_path / "state"
    units = tmp_path / "units"
    units.mkdir()
    # Bootstrap's job: unit dir starts in sync with the checkout.
    for f in (repo / "provision").glob("agent-ops-*"):
        (units / f.name).write_text(f.read_text())

    env = dict(
        os.environ,
        AGENT_OPS_REPO=str(repo),
        AGENT_OPS_STATE_DIR=str(state),
        AGENT_OPS_UNIT_DIR=str(units),
        AGENT_OPS_SYSTEMCTL=f"{sysctl} --user",
    )
    return SimpleNamespace(origin=origin, repo=repo, state=state,
                           units=units, calls=calls, env=env)


def run_update(box, **env_extra):
    return subprocess.run(
        ["bash", str(SCRIPT)], env={**box.env, **env_extra},
        capture_output=True, text=True)


def calls(box):
    return box.calls.read_text() if box.calls.exists() else ""


def head(repo):
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def test_noop_when_up_to_date(box):
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert calls(box) == ""


def test_code_only_change_pulls_without_restart_or_reinstall(box):
    (box.origin / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "code change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert head(box.repo) == head(box.origin)
    assert calls(box) == ""


def test_dep_change_reinstalls_package(box):
    (box.origin / "pyproject.toml").write_text(
        "[project]\nname = 'agent-ops'\ndependencies = ['pyyaml']\n")
    commit_all(box.origin, "dep change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "pip install -e ." in calls(box)


def test_unit_change_syncs_and_restarts_only_changed_unit(box):
    (box.origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v2\n")
    commit_all(box.origin, "unit change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "waitd v2" in (box.units / "agent-ops-waitd.service").read_text()
    log = calls(box)
    assert "systemctl --user daemon-reload" in log
    assert "try-restart agent-ops-waitd.service" in log
    assert "agent-ops-dispatcher.timer" not in log


def test_diverged_checkout_fails_without_moving_head(box):
    (box.repo / "local.txt").write_text("local\n")
    git(box.repo, "config", "user.email", "t@t")
    git(box.repo, "config", "user.name", "t")
    commit_all(box.repo, "local divergence")
    (box.origin / "app.py").write_text("x = 2\n")
    commit_all(box.origin, "remote change")
    local_head = head(box.repo)
    r = run_update(box)
    assert r.returncode != 0
    assert head(box.repo) == local_head


def test_respects_convergence_lock(box):
    box.state.mkdir(parents=True, exist_ok=True)
    lock = open(box.state / "convergence.lock", "w")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    try:
        r = run_update(box, AGENT_OPS_FLOCK_WAIT="0")
        assert r.returncode != 0
    finally:
        lock.close()
