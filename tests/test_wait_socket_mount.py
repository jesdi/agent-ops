"""The public session command must not let tasks replace the host listener."""
from pathlib import Path
import shlex

import pytest

from dispatcher.models import Entry
from dispatcher.sessions import podman_cmd


@pytest.mark.parametrize("model,second", [
    pytest.param("anthropic/claude-opus-5", None, id="claude"),
    pytest.param("openai/gpt-6-astra", None, id="codex"),
    pytest.param("anthropic/claude-opus-5", Entry("openai", "gpt-6-astra", "high"),
                 id="claude-with-codex-secondary"),
])
def test_session_wait_socket_directory_is_only_exposed_read_only(tmp_path, monkeypatch, model, second):
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/task-42\n")

    argv = shlex.split(podman_cmd("acme", 42, str(worktree), "2g", "2", model,
                                 "--prompt-file .agent/prompt-review.md", second=second))
    mounts = [argv[index + 1].split(":") for index, arg in enumerate(argv[:-1]) if arg == "-v"]
    wait = state / "wait"
    # Inspect every alias that could expose the directory, including a mount
    # of its parent. Merely adding a second read-only mount is insufficient.
    exposing_wait = [mount for mount in mounts if wait.is_relative_to(Path(mount[0]))]
    assert exposing_wait == [[str(wait), str(wait), "ro"]]
    assert wait.is_dir(), "host provisioning must still create the listener's directory"
    assert f"AGENT_OPS_STATE_DIR={state}" in argv, "hooks/controller must resolve the same existing socket"
    if second is not None:
        assert [str(state / "codex-home"), "/root/.codex"] in mounts
