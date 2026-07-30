"""E2E tests for provision/update.sh in a sandboxed git + fake-systemctl rig."""
import os
import shutil
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
    (origin / "Containerfile").write_text("FROM node:22-bookworm\n")
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
    podman = bin_dir / "podman"
    podman.write_text(f'#!/bin/sh\necho "podman $@" >> "{calls}"\n')
    podman.chmod(0o755)
    pnpm = bin_dir / "pnpm"
    pnpm.write_text(f'#!/bin/sh\necho "pnpm $@" >> "{calls}"\n')
    pnpm.chmod(0o755)

    # Seed + sync script + fake claude so the claude-home sync works.
    prov_src = Path(__file__).resolve().parent.parent / "provision"
    real_seed = prov_src / "claude-home"
    subprocess.run(["cp", "-R", str(real_seed),
                    str(origin / "provision" / "claude-home")], check=True)
    subprocess.run(["cp", str(prov_src / "claude-home-sync.sh"),
                    str(origin / "provision" / "claude-home-sync.sh")], check=True)
    commit_all(origin, "add claude-home seed")
    git(repo, "pull")

    plugin_list = tmp_path / "plugins.json"
    plugin_list.write_text(
        '[{"id": "superpowers@claude-plugins-official", "version": "4.0.0"}, '
        '{"id": "frontend-design@claude-plugins-official", "version": "1.0.0"}]')
    claude = bin_dir / "claude"
    claude.write_text(
        f'#!/bin/sh\necho "claude $@" >> "{calls}"\n'
        f'[ "$1 $2" = "plugin list" ] && cat "{plugin_list}"\nexit 0\n')
    claude.chmod(0o755)

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
        AGENT_OPS_PODMAN=str(podman),
        AGENT_OPS_PNPM=str(pnpm),
        AGENT_OPS_CLAUDE=str(claude),
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
    assert [l for l in calls(box).splitlines()
            if not l.startswith("claude")] == []


def test_code_only_change_pulls_without_restart_or_reinstall(box):
    (box.origin / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "code change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert head(box.repo) == head(box.origin)
    assert [l for l in calls(box).splitlines()
            if not l.startswith("claude")] == []


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


def test_unit_drift_heals_without_new_commits(box):
    # Installed unit drifts from the checkout (e.g. a manual pull carried the
    # unit change through a pass that saw old==new). Convergence must repair
    # actual state, not just react to rev deltas.
    (box.units / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=stale ExecStart\n")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "waitd v1" in (box.units / "agent-ops-waitd.service").read_text()
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


def test_containerfile_change_triggers_podman_build(box):
    (box.origin / "Containerfile").write_text(
        "FROM node:22-bookworm\nRUN true\n")
    commit_all(box.origin, "containerfile change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "podman build -t agent-ops-session -f Containerfile ." in calls(box)


def test_code_change_does_not_build_image(box):
    (box.origin / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "code change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "podman" not in calls(box)


def test_frontend_change_triggers_pnpm_build(box):
    fe = box.origin / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text('{"name": "frontend"}\n')
    commit_all(box.origin, "frontend change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    log = calls(box)
    assert "pnpm install --frozen-lockfile" in log
    assert "pnpm build" in log
    assert "try-restart agent-ops-web.service" in log


def seed_frontend(box):
    """Commit a frontend/ dir upstream and pull it into the box checkout."""
    fe = box.origin / "frontend"
    fe.mkdir()
    (fe / "package.json").write_text('{"name": "frontend"}\n')
    commit_all(box.origin, "add frontend")


def test_missing_frontend_dist_builds_without_new_commits(box):
    # Like unit sync and claude-home sync, the frontend build repairs actual
    # drift: a host cloned at HEAD never sees a rev delta, and a cleared dist
    # must heal. Otherwise web/app.py serves the "ui": "not built" stub forever.
    seed_frontend(box)
    r = run_update(box)  # rev delta: builds
    assert r.returncode == 0, r.stderr
    assert "pnpm build" in calls(box)

    # dist now present, no new commits — must NOT rebuild.
    (box.repo / "frontend" / "dist").mkdir(parents=True)
    box.calls.unlink()
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "pnpm" not in calls(box)

    # dist cleared by hand, still no new commits — must heal.
    shutil.rmtree(box.repo / "frontend" / "dist")
    box.calls.unlink()
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    log = calls(box)
    assert "pnpm install --frozen-lockfile" in log
    assert "pnpm build" in log
    assert "try-restart agent-ops-web.service" in log


def test_missing_pnpm_skips_build_without_failing_the_pass(box):
    # bootstrap.sh is one-shot: a box provisioned before the frontend existed
    # has no pnpm. `set -euo pipefail` would abort the pass at the build line
    # and take unit sync + claude-home sync down with it on every firing.
    seed_frontend(box)
    (box.units / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=stale ExecStart\n")
    r = run_update(box, AGENT_OPS_PNPM="agent-ops-nonexistent-pnpm")
    assert r.returncode == 0, r.stderr
    assert "pnpm" not in calls(box)
    assert "pnpm not found" in r.stderr
    # Everything downstream of the build still ran.
    assert "waitd v1" in (box.units / "agent-ops-waitd.service").read_text()
    assert (box.state / "claude-home" / "CLAUDE.md").exists()


def test_python_change_does_not_build_frontend(box):
    (box.origin / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "code change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "pnpm" not in calls(box)


def test_claude_home_synced_every_pass(box):
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    home = box.state / "claude-home"
    assert (home / "CLAUDE.md").exists()
    assert (home / "hooks" / "block-dangerous-git.sh").exists()


def test_claude_home_drift_heals_without_new_commits(box):
    run_update(box)
    (box.state / "claude-home" / "CLAUDE.md").write_text("# drifted\n")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "drifted" not in (box.state / "claude-home" / "CLAUDE.md").read_text()


def test_web_code_change_restarts_web_service(box):
    (box.origin / "web").mkdir()
    (box.origin / "web" / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "web change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "try-restart agent-ops-web.service" in calls(box)
    assert "pip install" not in calls(box)


def test_dispatcher_change_restarts_web_service(box):
    (box.origin / "dispatcher").mkdir()
    (box.origin / "dispatcher" / "state.py").write_text("x = 1\n")
    commit_all(box.origin, "dispatcher change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "try-restart agent-ops-web.service" in calls(box)


def test_unrelated_code_change_does_not_restart_web(box):
    (box.origin / "notes.md").write_text("x\n")
    commit_all(box.origin, "docs change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "agent-ops-web" not in calls(box)


def test_dep_and_web_change_installs_before_restart(box):
    (box.origin / "pyproject.toml").write_text(
        "[project]\nname = 'agent-ops'\ndependencies = ['pyyaml']\n")
    (box.origin / "web").mkdir()
    (box.origin / "web" / "app.py").write_text("x = 1\n")
    commit_all(box.origin, "dep and web change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    log = calls(box)
    pip_pos = log.find("pip install")
    restart_pos = log.find("try-restart agent-ops-web.service")
    assert pip_pos != -1, "pip install not called"
    assert restart_pos != -1, "try-restart not called"
    assert pip_pos < restart_pos, "pip install must precede try-restart"
