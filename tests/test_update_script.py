"""E2E tests for provision/update.sh in a sandboxed git + fake-systemctl rig."""
import os
import shutil
import subprocess
import time
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
    # The real units ship in the fake origin so the sync loop sees what the
    # box sees — notably the `agent-ops-alert@.service` TEMPLATE unit, which
    # systemd cannot try-restart without an instance name. Two of them are
    # then replaced by cheap stubs the change/drift tests mutate by hand.
    prov_repo = Path(__file__).resolve().parent.parent / "provision"
    for real in sorted(prov_repo.glob("agent-ops-*")):
        (origin / "provision" / real.name).write_text(real.read_text())
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
    # Mimics real systemd: `try-restart` on a template unit with no instance
    # is rejected ("missing the instance name") with a non-zero exit, which
    # under `set -euo pipefail` would abort the whole update pass.
    sysctl.write_text(
        f'#!/bin/sh\n'
        f'echo "systemctl $*" >> "{calls}"\n'
        f'case " $* " in\n'
        f'  *" try-restart "*)\n'
        f'    case "$*" in\n'
        f'      *@.service*)\n'
        f'        echo "Unit name is missing the instance name." >&2\n'
        f'        exit 1 ;;\n'
        f'    esac ;;\n'
        f'esac\n'
        f'exit 0\n')
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
    # herdr is present on a converged box; the fake is silent so the
    # "no calls" assertions above stay exact.
    herdr = bin_dir / "herdr"
    herdr.write_text('#!/bin/sh\necho "herdr 0.8.2"\n')
    herdr.chmod(0o755)
    # tmux: the one-shot migration is gated on an agent-ops-shaped session
    # (`task-*` / `triage`) appearing in `tmux ls -F '#{session_name}'`, so
    # the fake just prints the names in a file the migration tests write.
    # Default: empty, i.e. a live tmux server with nothing of ours in it.
    # Silent like herdr, so the "no calls" assertions stay exact.
    tmux_sessions = tmp_path / "tmux-sessions"
    tmux_sessions.write_text("")
    tmux = bin_dir / "tmux"
    tmux.write_text(
        f'#!/bin/sh\n[ "$1" = "ls" ] && exec cat "{tmux_sessions}"\nexit 0\n')
    tmux.chmod(0o755)
    # The dispatcher entrypoint the migration invokes. Recording argv is the
    # whole point: the test asserts on the flag and the --config path.
    py = venv_bin / "python"
    py.write_text(f'#!/bin/sh\necho "python $@" >> "{calls}"\n')
    py.chmod(0o755)

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
        AGENT_OPS_HERDR=str(herdr),
        AGENT_OPS_TMUX=str(tmux),
        AGENT_OPS_CLAUDE=str(claude),
        # Point the creds-convergence step at a non-existent path so
        # pre-existing tests are a clean no-op regardless of the developer's
        # real ~/.claude store. Tests that need a host file override this via
        # _creds_setup or by setting box.env["AGENT_OPS_HOST_CREDS"] directly.
        AGENT_OPS_HOST_CREDS=str(tmp_path / "nohost" / ".credentials.json"),
    )
    return SimpleNamespace(origin=origin, repo=repo, state=state,
                           units=units, calls=calls, env=env,
                           tmux_sessions=tmux_sessions)


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


def test_template_unit_change_syncs_without_aborting_the_pass(box):
    # `systemctl try-restart agent-ops-alert@.service` is rejected by systemd
    # (no instance name) and, under `set -euo pipefail`, would kill the pass —
    # on the very pass that deploys the template unit, before the keepalive
    # timer restart, credential convergence and claude-home sync. The template
    # must still be copied and daemon-reloaded, just never try-restarted.
    tmpl = "agent-ops-alert@.service"
    assert (box.origin / "provision" / tmpl).exists(), "fixture must ship it"
    (box.origin / "provision" / tmpl).write_text(
        "[Unit]\nDescription=alert v2 for %i\n")
    (box.origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v2\n")
    commit_all(box.origin, "template + regular unit change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    # Copied and daemon-reloaded...
    assert "alert v2" in (box.units / tmpl).read_text()
    log = calls(box)
    assert "systemctl --user daemon-reload" in log
    # ...but never try-restarted...
    assert f"try-restart {tmpl}" not in log
    # ...and everything downstream of the restart loop still ran.
    assert "try-restart agent-ops-waitd.service" in log
    assert (box.state / "claude-home" / "CLAUDE.md").exists()


def test_oneshot_unit_change_is_synced_but_never_restarted(box):
    # 2026-09-03 deploy deadlock: this script holds convergence.lock for the
    # whole pass, and `try-restart` of a RUNNING oneshot (the dispatcher pass)
    # starts a replacement that blocks in pass_lock on that same file while
    # try-restart waits for it to finish — the pass hung for 10 hours, both
    # timers stopped firing, and the next merge was never pulled. A oneshot
    # needs no restart anyway: each timer firing runs the current checkout
    # under the freshly reloaded unit. So a oneshot is copied and
    # daemon-reloaded, never try-restarted; long-running units still are.
    unit = "agent-ops-dispatcher.service"
    src = box.origin / "provision" / unit
    assert "Type=oneshot" in src.read_text(), "fixture must ship the real unit"
    src.write_text(src.read_text() + "# deploy comment v2\n")
    (box.origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v2\n")
    commit_all(box.origin, "oneshot + long-running unit change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "deploy comment v2" in (box.units / unit).read_text()
    log = calls(box)
    assert "systemctl --user daemon-reload" in log
    assert f"try-restart {unit}" not in log
    assert "try-restart agent-ops-waitd.service" in log
    assert (box.state / "claude-home" / "CLAUDE.md").exists()


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


def seed_credentials(box, body=None, token=True):
    """Commit a fake provision/credentials.sh upstream; optionally place the
    box's op-token.env so the credentials pass is armed."""
    script = box.origin / "provision" / "credentials.sh"
    script.write_text(body or f'#!/bin/sh\necho "credentials ran" >> "{box.calls}"\n')
    commit_all(box.origin, "credentials script")
    if token:
        box.state.mkdir(parents=True, exist_ok=True)
        (box.state / "op-token.env").write_text("OP_SERVICE_ACCOUNT_AGENT_OPS_TOKEN=x\n")


def test_credentials_run_when_script_first_appears(box):
    # The regression this guards: the signing feature merged into
    # credentials.sh, but nothing on the box ever re-ran it (bootstrap is
    # one-shot), so commits stayed unsigned until someone SSHed in.
    seed_credentials(box)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "credentials ran" in calls(box)


def test_credentials_not_rerun_when_unchanged(box):
    seed_credentials(box)
    run_update(box)
    box.calls.unlink()
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "credentials ran" not in calls(box)


def test_credentials_rerun_on_content_change(box):
    seed_credentials(box)
    run_update(box)
    box.calls.unlink()
    (box.origin / "provision" / "credentials.sh").write_text(
        f'#!/bin/sh\necho "credentials ran v2" >> "{box.calls}"\n')
    commit_all(box.origin, "credentials v2")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "credentials ran v2" in calls(box)


def test_missing_token_skips_credentials_without_failing(box):
    # A box before its one manual secret is placed has nothing to
    # materialize; failing the pass would take unit sync down every firing.
    seed_credentials(box, token=False)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "credentials" not in calls(box)
    assert "op-token.env" in r.stderr


def test_failed_credentials_fails_pass_and_retries_next_pass(box):
    # A transient 1P failure must surface loudly AND not be stamped as
    # converged — the next firing retries.
    seed_credentials(box, body="#!/bin/sh\nexit 1\n")
    r = run_update(box)
    assert r.returncode != 0
    (box.origin / "provision" / "credentials.sh").write_text(
        f'#!/bin/sh\necho "credentials ran" >> "{box.calls}"\n')
    commit_all(box.origin, "fix credentials")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "credentials ran" in calls(box)


# ---------------------------------------------------------------------------
# Task 4: credential convergence — freshest valid host login -> claude-home
# ---------------------------------------------------------------------------

VALID_CREDS = '{"claudeAiOauth": {"accessToken": "sk-live", "expiresAt": 1}}'


def _creds_setup(box, tmp_path, host_body, host_newer=True):
    """Write a host credentials file and seed claude-home with a stale copy."""
    host = tmp_path / "hostclaude" / ".credentials.json"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(host_body)
    ch = box.state / "claude-home" / ".credentials.json"
    ch.parent.mkdir(parents=True, exist_ok=True)
    ch.write_text('{"claudeAiOauth": {"accessToken": "sk-stale"}}')
    stamp = time.time() + (60 if host_newer else -60)
    os.utime(host, (stamp, stamp))
    box.env["AGENT_OPS_HOST_CREDS"] = str(host)
    return host, ch


def test_newer_valid_host_creds_converge_into_claude_home(box, tmp_path):
    _, ch = _creds_setup(box, tmp_path, VALID_CREDS, host_newer=True)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-live" in ch.read_text()
    assert (ch.stat().st_mode & 0o777) == 0o600


def test_convergence_is_atomic_and_leaves_no_temp_file(box, tmp_path):
    # Readers (dispatcher, keepalive, session containers) must never see a
    # truncated credentials file, so the copy lands via temp-then-rename —
    # and the temp must not survive the pass.
    _, ch = _creds_setup(box, tmp_path, VALID_CREDS, host_newer=True)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-live" in ch.read_text()
    assert not Path(str(ch) + ".tmp").exists()


def test_older_host_creds_left_alone(box, tmp_path):
    _, ch = _creds_setup(box, tmp_path, VALID_CREDS, host_newer=False)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-stale" in ch.read_text()


def test_corrupt_host_creds_never_copied(box, tmp_path):
    _, ch = _creds_setup(box, tmp_path, "not json {", host_newer=True)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-stale" in ch.read_text()


def test_tokenless_host_creds_never_copied(box, tmp_path):
    _, ch = _creds_setup(box, tmp_path,
                         '{"claudeAiOauth": {"accessToken": ""}}',
                         host_newer=True)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-stale" in ch.read_text()


def test_missing_host_creds_is_a_noop(box, tmp_path):
    box.env["AGENT_OPS_HOST_CREDS"] = str(tmp_path / "nope" / "creds.json")
    r = run_update(box)
    assert r.returncode == 0, r.stderr  # must not fail under set -e


def test_array_host_creds_never_copied_and_no_traceback(box, tmp_path):
    # A valid JSON non-dict (e.g. []) must be silently skipped — no copy, no
    # Python traceback noise in the updater log (AttributeError from .get()).
    _, ch = _creds_setup(box, tmp_path, "[]", host_newer=True)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "sk-stale" in ch.read_text()
    assert "Traceback" not in r.stderr
    assert "AttributeError" not in r.stderr


def test_failed_install_aborts_pass_and_does_not_print_converged(box, tmp_path):
    # Regression: the former `install … && mv` AND-list swallowed install
    # failures under `set -e`, letting the pass continue and print the
    # "converged" success line when nothing was actually written.  With two
    # plain statements the first failure must abort immediately.
    host, ch = _creds_setup(box, tmp_path, VALID_CREDS, host_newer=True)
    # Make the claude-home dir unwritable so `install` cannot create the temp.
    ch_dir = box.state / "claude-home"
    ch_dir.mkdir(parents=True, exist_ok=True)
    ch_dir.chmod(0o555)
    try:
        r = run_update(box)
        assert r.returncode != 0, (
            "update pass must exit non-zero when install fails; got 0\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "converged fresher host credentials" not in r.stdout, (
            "success line must not appear when install failed"
        )
    finally:
        ch_dir.chmod(0o755)


def test_first_ever_convergence_creates_dir_and_sets_mode(box, tmp_path):
    # claude-home/.credentials.json does not yet exist (and its parent dir may
    # not exist either). A valid, newer host file must be copied in and land
    # with mode 0600, exercising the `mkdir -p` and the `[ ! -f "$CH_CREDS" ]`
    # branch.
    host = tmp_path / "hostclaude" / ".credentials.json"
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(VALID_CREDS)
    # Do NOT pre-create claude-home or the credentials file.
    ch = box.state / "claude-home" / ".credentials.json"
    assert not ch.exists(), "precondition: ch must not exist before the run"
    box.env["AGENT_OPS_HOST_CREDS"] = str(host)
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert ch.exists(), "claude-home/.credentials.json was not created"
    assert "sk-live" in ch.read_text()
    assert (ch.stat().st_mode & 0o777) == 0o600


def test_herdr_present_is_left_alone(box):
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "herdr-installer" not in calls(box)


def test_missing_herdr_is_installed_before_unit_sync(box, tmp_path):
    missing = tmp_path / "nowhere" / "herdr"
    installer = tmp_path / "bin" / "herdr-installer"
    installer.write_text(
        f'#!/bin/sh\necho "herdr-installer HERDR_INSTALL_DIR=$HERDR_INSTALL_DIR" >> "{box.calls}"\n'
        f'mkdir -p "$HERDR_INSTALL_DIR"\n'
        f'printf \'#!/bin/sh\\necho herdr 0.8.2\\n\' > "$HERDR_INSTALL_DIR/herdr"\n'
        f'chmod +x "$HERDR_INSTALL_DIR/herdr"\n')
    installer.chmod(0o755)
    r = run_update(box, AGENT_OPS_HERDR=str(missing),
                   AGENT_OPS_HERDR_INSTALL=str(installer))
    assert r.returncode == 0, r.stderr
    assert f"herdr-installer HERDR_INSTALL_DIR={missing.parent}" in calls(box)
    assert missing.exists()


def test_failed_herdr_install_aborts_before_unit_sync(box, tmp_path):
    """Unlike a missing pnpm (warn and skip), code that needs herdr has
    already been pulled: a failed install must fail the pass loudly, and
    before the unit sync so no unit is restarted onto a box without it."""
    installer = tmp_path / "bin" / "herdr-installer"
    installer.write_text('#!/bin/sh\necho "installer exploded" >&2\nexit 1\n')
    installer.chmod(0o755)
    (box.origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v2\n")
    commit_all(box.origin, "unit change")
    r = run_update(box, AGENT_OPS_HERDR=str(tmp_path / "nowhere" / "herdr"),
                   AGENT_OPS_HERDR_INSTALL=str(installer))
    assert r.returncode != 0
    assert "installer exploded" in r.stderr
    assert "daemon-reload" not in calls(box)
    assert "waitd v1" in (box.units / "agent-ops-waitd.service").read_text()


# --- one-shot tmux → herdr migration (delete with dispatcher/tmux_migration.py)

def test_tmux_migration_runs_once_when_tmux_has_sessions(box):
    """Sessions the pre-herdr dispatcher left in tmux are handed to the
    park/resume path BEFORE any unit restart, so the dispatcher's next pass
    already sees them queued for a herdr resume."""
    box.tmux_sessions.write_text("task-acme-42\n")
    (box.origin / "provision" / "agent-ops-waitd.service").write_text(
        "[Unit]\nDescription=waitd v2\n")
    commit_all(box.origin, "unit change")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    log = calls(box).splitlines()
    migrations = [i for i, l in enumerate(log) if "--migrate-tmux" in l]
    assert len(migrations) == 1, log
    assert f"--config {box.state}/targets.yaml" in log[migrations[0]]
    restarts = [i for i, l in enumerate(log) if "try-restart" in l]
    assert restarts, log
    assert migrations[0] < restarts[0], log


def test_no_tmux_migration_without_sessions(box):
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "--migrate-tmux" not in calls(box)


def test_no_tmux_migration_for_an_operators_own_session(box):
    """The guard is on a `task-*`/`triage` session, not on a tmux server
    being up: an operator's own tmux as `agent` must not re-run the
    migration on every convergence pass, forever."""
    box.tmux_sessions.write_text("foo\n0\nmytask\n")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "--migrate-tmux" not in calls(box)


def test_tmux_migration_runs_for_a_live_triage_session(box):
    box.tmux_sessions.write_text("foo\ntriage\n")
    r = run_update(box)
    assert r.returncode == 0, r.stderr
    assert "--migrate-tmux" in calls(box)
