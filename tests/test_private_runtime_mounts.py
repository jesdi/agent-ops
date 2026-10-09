"""Private runtime authority must stay outside every generated container bind.

Exercise public command builders with disposable path layouts only. No runtime
credential or snapshot contents need to exist for launch validation to reject a bind.
"""
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest
import yaml
import websockets

from dispatcher import containers

from dispatcher.config import load_config
from dispatcher.models import Entry
from dispatcher.sessions import podman_cmd


MODELS = ["anthropic/claude-opus-5", "openai/gpt-6-astra"]


def worktree_at(path, clone):
    path.mkdir(parents=True)
    (path / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    return str(path)


@pytest.mark.parametrize("model", MODELS)
def test_configured_state_inside_clone_prevents_session_launch(tmp_path, monkeypatch, model):
    clone = tmp_path / "clone"
    state = clone / "host-state"
    target = dict(name="acme", repo="owner/acme", clone_path=str(clone),
                  worktrees_path=str(tmp_path / "worktrees"), rank_cmd="rank",
                  gate_cmd="make gate", project_number=1, project_owner="owner",
                  status_field_id="status", status_ready_option_id="ready",
                  status_in_progress_option_id="active")
    config = tmp_path / "targets.yaml"
    config.write_text(yaml.safe_dump(dict(state_dir=str(state), targets=[target])))
    monkeypatch.delenv("AGENT_OPS_STATE_DIR")
    cfg = load_config(config)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", cfg.state_dir)
    worktree = worktree_at(Path(cfg.targets[0].worktrees_path) / "task-42",
                          cfg.targets[0].clone_path)

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        podman_cmd("acme", 42, worktree, "2g", "2", model, "--prompt-file .agent/prompt.md")


GRANTS = [
    pytest.param(MODELS[0], None, "claude-home", id="claude-primary"),
    pytest.param(MODELS[1], None, "codex-home", id="codex-primary"),
    pytest.param(MODELS[0], Entry("openai", "gpt-6-astra", "high"), "codex-home",
                 id="codex-secondary"),
    pytest.param(MODELS[1], Entry("anthropic", "claude-opus-5", "high"), "claude-home",
                 id="claude-secondary"),
]


@pytest.mark.parametrize("model,second,home", GRANTS)
@pytest.mark.parametrize("private", [".", "runtime/task", "runtime-host-token"])
def test_provider_home_alias_cannot_grant_private_state(tmp_path,
                                                       model, second, home, private):
    state = tmp_path / "state"
    state.mkdir()
    (state / home).symlink_to(state / private)
    worktree = worktree_at(tmp_path / "worktree", tmp_path / "clone")

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        podman_cmd("acme", 42, worktree, "2g", "2", model,
                   "--prompt-file .agent/prompt.md", second=second)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("source", ["claude-binary", "codex-package", "gitconfig", "gh"])
def test_readonly_support_mounts_cannot_expose_private_state(
        tmp_path, monkeypatch, codex_package, model, source):
    state = tmp_path / "state"
    home = Path.home()
    second = None
    if source == "claude-binary":
        (home / ".local/bin/claude").symlink_to(state / "runtime-host-token")
        if model == MODELS[1]:
            second = Entry("anthropic", "claude-opus-5", "high")
    elif source == "codex-package":
        state = codex_package / "host-state"
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
        if model == MODELS[0]:
            second = Entry("openai", "gpt-6-astra", "high")
    elif source == "gitconfig":
        (home / ".gitconfig").symlink_to(state / "runtime-host-token")
    else:
        (home / ".config").mkdir()
        (home / ".config/gh").symlink_to(state / "runtime")
    worktree = worktree_at(tmp_path / "worktree", tmp_path / "clone")

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        podman_cmd("acme", 42, worktree, "2g", "2", model,
                   "--prompt-file .agent/prompt.md", second=second)


@pytest.mark.parametrize("location", ["relay", "dependency"])
def test_relocated_supervisor_install_cannot_include_private_state(tmp_path, location):
    # Real package-location discovery in a disposable installation. No mocks of
    # mount selection, and no state is written in the developer's installation.
    source = tmp_path / "relay"
    dependency = tmp_path / "dependency"
    shutil.copytree(Path(containers.__file__).parent, source / "dispatcher",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(Path(websockets.__file__).parent, dependency / "websockets",
                    ignore=shutil.ignore_patterns("__pycache__"))
    state = tmp_path / location / "host-state"
    worktree = worktree_at(tmp_path / "worktree", tmp_path / "clone")
    env = {"HOME": str(Path.home()), "AGENT_OPS_STATE_DIR": str(state),
           "PYTHONPATH": os.pathsep.join([str(source), str(dependency)])}
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from dispatcher.sessions import podman_cmd; "
         "podman_cmd('acme', 42, sys.argv[1], '2g', '2', 'openai/gpt-6-astra', '')",
         worktree], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)

    assert result.returncode != 0, "a read-only installation bind still grants access to private state"
    assert "overlaps private runtime state" in result.stderr


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("layout", ["git-selects-state", "clone-alias", "state-alias",
                                    "worktree-ancestor", "private-runtime-alias", "private-token-alias"])
def test_session_launch_rejects_canonical_private_state_overlaps(tmp_path, monkeypatch,
                                                               model, layout):
    state = tmp_path / "state"
    state.mkdir()
    clone = tmp_path / "clone"
    worktree = tmp_path / "worktree"
    if layout == "git-selects-state":
        clone = state
    elif layout == "clone-alias":
        clone.symlink_to(state)
    elif layout == "state-alias":
        clone.mkdir()
        (tmp_path / "state-alias").symlink_to(clone)
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state-alias"))
    elif layout == "worktree-ancestor":
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(worktree / "host-state"))
    elif layout == "private-runtime-alias":
        (state / "runtime").symlink_to(clone / "private-runtime")
    else:
        (state / "runtime-host-token").symlink_to(clone / "private-token")
    task = worktree_at(worktree, clone)

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        podman_cmd("acme", 42, task, "2g", "2", model, "--prompt-file .agent/prompt.md")


@pytest.mark.parametrize("model,second,home", GRANTS)
def test_separate_layout_retains_provider_grants_and_readonly_wait(
        tmp_path, model, second, home):
    state = tmp_path / "state"
    state.mkdir()
    actual_home = tmp_path / "provider-data"
    actual_home.mkdir()
    (state / home).symlink_to(actual_home)
    # A common string prefix is not ancestry; paths need not exist yet.
    clone = tmp_path / "state-other"
    worktree = worktree_at(tmp_path / "worktree", clone)
    argv = shlex.split(podman_cmd("acme", 42, worktree, "2g", "2", model,
                                 "--prompt-file .agent/prompt.md", second=second))
    mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]
    assert f"{state}/wait:{state}/wait:ro" in mounts
    assert f"{clone}:{clone}" in mounts
    assert f"{worktree}:{worktree}" in mounts
    if model == MODELS[0] or second:
        claude_home = actual_home if home == "claude-home" else state / "claude-home"
        assert f"{claude_home}:/root/.claude" in mounts
    if model == MODELS[1] or second:
        codex_home = actual_home if home == "codex-home" else state / "codex-home"
        assert f"{codex_home}:/root/.codex" in mounts


@pytest.mark.parametrize("builder", ["setup", "triage"])
@pytest.mark.parametrize("source", ["clone", "workdir"])
def test_other_container_builders_reject_private_state_binds(tmp_path, builder, source):
    state = tmp_path / "state"
    clone = state if source == "clone" else tmp_path / "clone"
    workdir = state / "runtime/task" if source == "workdir" else tmp_path / "task"

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        if builder == "setup":
            worktree = worktree_at(workdir, clone)
            containers.setup_cmd("task-42-setup", worktree, "true")
        else:
            containers.triage_cmd("triage-acme", str(clone), str(workdir),
                                  "2g", "2", MODELS[0], "/triage/prompt.md")


@pytest.mark.parametrize("model,second,home", GRANTS)
def test_provider_home_alias_cannot_make_wait_directory_writable(tmp_path, model, second, home):
    state = tmp_path / "state"
    state.mkdir()
    (state / home).symlink_to(state / "wait")
    worktree = worktree_at(tmp_path / "worktree", tmp_path / "clone")

    with pytest.raises(ValueError, match="overlaps the read-only wait directory"):
        podman_cmd("acme", 42, worktree, "2g", "2", model, "", second=second)


@pytest.mark.parametrize("model", MODELS)
def test_wait_directory_alias_cannot_include_private_state(tmp_path, model):
    state = tmp_path / "state"
    state.mkdir()
    (state / "wait").symlink_to(state)
    worktree = worktree_at(tmp_path / "worktree", tmp_path / "clone")

    with pytest.raises(ValueError, match="overlaps"):
        podman_cmd("acme", 42, worktree, "2g", "2", model, "")


@pytest.mark.parametrize("location", ["clone", "private-runtime", "provider-home"])
def test_unresolvable_paths_fail_closed_before_command_return(tmp_path, location):
    state = tmp_path / "state"
    state.mkdir()
    clone = tmp_path / "clone"
    paths = {"clone": clone, "private-runtime": state / "runtime",
             "provider-home": state / "claude-home"}
    path = paths[location]
    path.symlink_to(path)
    worktree = worktree_at(tmp_path / "worktree", clone)

    with pytest.raises((OSError, RuntimeError)):
        podman_cmd("acme", 42, worktree, "2g", "2", MODELS[0], "")
