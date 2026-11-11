"""Issue #17: a setup container that outlives the provisioning timeout must
not be left running. Exercises the REAL create_workspace and the REAL
subprocess path; only `podman` is a fake on PATH that behaves like the real
client: `run --name N` registers N and runs the setup line (the last argv
word, as `sh -c` does in the container); killing the client (what
subprocess.run's timeout does) leaves N registered and its process running,
exactly as SIGKILL is not proxied to a real container. `rm -f` / `kill` /
`stop` kill the process and free the name; a setup that ends frees it too
(`--rm`). The registry is one file per live container name, holding its pid.
`FAKE_PODMAN_RM_EXIT` makes `rm` exit with that status, removing nothing.
"""
import dataclasses
import os
import signal
import stat
import subprocess
from pathlib import Path

import pytest

import dispatcher.workspace as workspace
from dispatcher import containers
from tests.test_workspace import repository

NAME = "task-portfolio_eval-42-setup"
HANG = "exec sleep 3600"
PARTIAL = "echo partial out; echo partial err >&2; exec sleep 3600"

FAKE_PODMAN = """#!/bin/sh
reg="$FAKE_PODMAN_REGISTRY"
echo "$*" >> "$FAKE_PODMAN_CALLS"
cmd="$1"; shift
case "$cmd" in
  run)
    name=""
    for a in "$@"; do setup="$a"; done
    while [ $# -gt 0 ]; do
      case "$1" in --name) name="$2"; shift;; esac
      shift
    done
    if [ -e "$reg/$name" ]; then
      echo "Error: the container name \\"$name\\" is already in use" >&2; exit 125
    fi
    sh -c "$setup" &
    pid=$!
    echo "$pid" > "$reg/$name"
    wait "$pid"; st=$?
    rm -f "$reg/$name"
    exit "$st" ;;
  rm|kill|stop)
    [ -n "$FAKE_PODMAN_RM_EXIT" ] && exit "$FAKE_PODMAN_RM_EXIT"
    for a in "$@"; do case "$a" in -*) ;; *)
      [ -e "$reg/$a" ] && kill "$(cat "$reg/$a")" 2>/dev/null
      rm -f "$reg/$a";; esac; done ;;
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
    monkeypatch.setenv("FAKE_PODMAN_CALLS", str(tmp_path / "podman-calls"))
    yield registry
    for entry in registry.iterdir():  # a container the code under test left running
        try:
            os.kill(int(entry.read_text()), signal.SIGKILL)
        except (ValueError, ProcessLookupError):
            pass


@pytest.fixture
def run(tmp_path, monkeypatch, fake_podman):
    """create_workspace with git stubbed out, the 1800 s provisioning timeout
    shrunk to 1 s, and every subprocess.run recorded as (argv, timeout)."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    real_sh = workspace._sh
    real_run = subprocess.run
    calls = []
    sh_timeouts = []

    def sh(args, cwd, timeout=300, log=None, env=None):
        if args[0] == "git":  # git is not under test: create the worktree dir
            if args[:3] == ["git", "worktree", "add"]:
                wt = Path(args[-2])
                wt.mkdir(parents=True, exist_ok=True)
                (wt / ".git").write_text(
                    f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")
            return
        sh_timeouts.append((list(args), timeout))
        real_sh(args, cwd, timeout=1 if timeout == 1800 else timeout, log=log, env=env)

    def recording_run(args, *a, **kw):
        calls.append((list(args), kw.get("timeout")))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(workspace, "_sh", sh)
    monkeypatch.setattr(subprocess, "run", recording_run)
    # The retry reuses the worktree: its git checks are not under test either.
    monkeypatch.setattr(workspace, "_worktree_health_issue", lambda wt, branch: None)
    monkeypatch.setattr(workspace, "_fetch_base", lambda *a: "refs/remotes/origin/main")
    monkeypatch.setattr(workspace, "_check_reused_base", lambda *a: None)
    monkeypatch.setattr(workspace, "_advance_to_base", lambda *a: None)

    def go(setup):
        t = dataclasses.replace(repository(tmp_path), setup_cmd=setup)
        return workspace.create_workspace(t, 42)

    go.calls = calls
    go.sh_timeouts = sh_timeouts
    go.log = tmp_path / "repo.worktrees" / "task-42" / ".agent" / "setup.log"
    return go


def _cleanup_calls(calls):
    return [(a, t) for a, t in calls
            if a[:1] == ["podman"] and a[1:2] in (["rm"], ["kill"], ["stop"])]


def test_setup_timeout_removes_the_setup_container(run, fake_podman):
    with pytest.raises(subprocess.TimeoutExpired):
        run(HANG)

    assert sorted(p.name for p in fake_podman.iterdir()) == [], \
        "the timed-out setup container must be stopped and its name freed"


def test_partial_log_survives_and_removal_has_its_own_timeout(run):
    with pytest.raises(subprocess.TimeoutExpired):
        run(PARTIAL)

    assert run.log.read_text() == "partial out\npartial err\n"
    assert (["podman", "rm", "-f", NAME], 60) in _cleanup_calls(run.calls), \
        "the setup container must be force-removed with a 60 s host timeout"


@pytest.mark.parametrize("failure", ["exit 1", "TimeoutExpired", "OSError"])
def test_removal_failure_does_not_mask_the_timeout(run, monkeypatch, failure):
    if failure == "exit 1":
        monkeypatch.setenv("FAKE_PODMAN_RM_EXIT", "1")
    else:
        recording_run = subprocess.run

        def failing_rm(args, *a, **kw):
            if list(args[:2]) == ["podman", "rm"]:
                run.calls.append((list(args), kw.get("timeout")))
                if failure == "OSError":
                    raise FileNotFoundError(2, "No such file or directory", "podman")
                raise subprocess.TimeoutExpired(args, kw.get("timeout"))
            return recording_run(args, *a, **kw)

        monkeypatch.setattr(subprocess, "run", failing_rm)

    with pytest.raises(subprocess.TimeoutExpired) as exc:
        run(PARTIAL)

    assert _cleanup_calls(run.calls), "the removal must have been attempted"
    assert exc.value.cmd[:2] == ["podman", "run"], \
        f"the original setup timeout must propagate, got {exc.value.cmd!r}"
    assert run.log.read_text() == "partial out\npartial err\n"


def test_retry_after_timeout_reuses_the_container_name(run, tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run(HANG)

    wt = run("true")

    assert wt == str(tmp_path / "repo.worktrees" / "task-42")


def test_setup_cmd_carries_podman_run_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = str(tmp_path / "wt"), str(tmp_path / "repo")
    argv = containers.setup_cmd(NAME, wt, "scripts/setup-worktree.sh", clone)
    image = argv.index("agent-ops-session")
    assert "--timeout" in argv[:image], f"--timeout must precede the image: {argv}"
    assert argv[argv.index("--timeout") + 1] == "1800"


def test_host_timeout_matches_podman_run_timeout(run):
    run("true")

    [(argv, timeout)] = [(a, t) for a, t in run.sh_timeouts if a[:2] == ["podman", "run"]]
    assert "--timeout" in argv, f"the setup argv carries no podman timeout: {argv}"
    assert timeout == 1800 == int(argv[argv.index("--timeout") + 1])


def test_setup_that_succeeds_needs_no_cleanup(run, tmp_path):
    assert run("echo ok") == str(tmp_path / "repo.worktrees" / "task-42")
    assert _cleanup_calls(run.calls) == []


def test_setup_that_fails_needs_no_cleanup(run):
    with pytest.raises(subprocess.CalledProcessError):
        run("echo broke; exit 1")
    assert _cleanup_calls(run.calls) == []
    assert run.log.read_text() == "broke\n"
