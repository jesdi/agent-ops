"""Stable command paths across dispatcher and terminal working directories."""
import os
from pathlib import Path
import shlex

import pytest

from dispatcher import containers
from dispatcher.sessions import Sessions
from tests.runtime_listener import launch_listener, seed_resume_task  # noqa: F401
from tests.test_sessions import LIVE, herdr_fake, herdr_fake_creating


def ordinary_worktree(root):
    worktree = root / "worktree"
    worktree.mkdir(parents=True)
    gitdir = root / "clone" / ".git" / "worktrees" / "task-42"
    gitdir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    return worktree


@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
def test_relative_session_paths_survive_dispatcher_and_terminal_cwd_changes(
        tmp_path, monkeypatch, launch_listener, resume):
    environment_state = tmp_path / "environment-state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(environment_state))
    monkeypatch.chdir(tmp_path)
    session = Sessions(state_dir="private state")
    state = tmp_path / "private state"
    worktree = ordinary_worktree(tmp_path / "launch-from-here")
    monkeypatch.chdir(worktree.parent)
    calls = []
    prompt = "The exact prompt after changing directory."
    if resume:
        seed_resume_task(state, str(worktree))
        herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
        session.resume("acme", 42, "worktree", prompt, "openai/gpt-6-astra",
                       session_id="recorded-conversation")
    else:
        herdr_fake_creating(monkeypatch, calls)
        session.spawn_stage("acme", 42, "worktree", prompt, "review", "openai/gpt-6-astra")

    # herdr runs the command from the task directory, after command construction.
    monkeypatch.chdir(worktree)
    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    words = shlex.split(command)
    assert f"AGENT_OPS_STATE_DIR={state}" in words
    assert f"{state}/wait:{state}/wait:ro" in words
    assert f"{state}/codex-home:/root/.codex" in words
    assert words[words.index("-w") + 1] == str(worktree)
    prompt_path = Path(worktree, words[words.index("--prompt-file") + 1])
    assert prompt_path.read_text() == prompt
    view = session.runtime_view("acme", 42)
    assert view["binding"]["worktree"] == str(worktree)
    assert os.environ["AGENT_OPS_STATE_DIR"] == str(environment_state)


@pytest.mark.parametrize("builder", ["session", "setup"])
def test_relative_gitdir_retains_a_usable_clone_mount_for_worktree_alias(tmp_path, builder):
    worktree = ordinary_worktree(tmp_path / "actual")
    (worktree / ".git").write_text("gitdir: ../clone/.git/worktrees/task-42\n")
    alias = tmp_path / "aliases" / "task"
    alias.parent.mkdir()
    alias.symlink_to(worktree)

    if builder == "session":
        words = shlex.split(containers.session_cmd(
            "task-acme-42", str(alias), "2g", "2", "claude-opus-5", "P"))
    else:
        words = containers.setup_cmd("task-acme-42-setup", str(alias), "true")

    # On the box, '..' follows the worktree symlink; inside the container it
    # is relative to the alias mount. Both sides must select the same clone.
    assert f"{worktree}:{alias}" in words
    assert f"{tmp_path}/actual/clone:{tmp_path}/aliases/clone" in words
    assert words[words.index("-w") + 1] == str(alias)
