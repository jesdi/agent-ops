from pathlib import Path

from dispatcher import containers


def make_worktree(tmp_path: Path) -> tuple[str, str]:
    clone = tmp_path / "repos" / "pe"
    wt = tmp_path / "repos" / "pe.worktrees" / "task-42"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    return str(wt), str(clone)


def test_clone_root_reads_gitdir_pointer(tmp_path: Path):
    wt, clone = make_worktree(tmp_path)
    assert containers.clone_root(wt) == clone


def test_image_env_override(monkeypatch):
    monkeypatch.delenv("AGENT_OPS_SESSION_IMAGE", raising=False)
    assert containers.image() == "agent-ops-session"
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "custom-img")
    assert containers.image() == "custom-img"


def test_session_cmd_mounts_worktree_clone_and_claude_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5",
                                 "--continue 'hi'")
    assert cmd.startswith("podman run --rm -it --name task-42 ")
    assert "--memory 2g --cpus 2" in cmd
    assert f"-v {wt}:{wt}" in cmd and f"-w {wt}" in cmd
    assert f"-v {clone}:{clone}" in cmd
    assert "-v /home/agent/agent-ops-state/claude-home:/root/.claude" in cmd
    assert cmd.endswith(
        "claude --permission-mode acceptEdits --model claude-fable-5 --continue 'hi'")


def test_session_cmd_carries_the_model_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-sonnet-4-6", "P")
    assert "--model claude-sonnet-4-6" in cmd


def test_setup_cmd_is_one_shot_with_cache_volumes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    argv = containers.setup_cmd("task-42-setup", wt, "scripts/provision-worktree.sh")
    assert argv[:3] == ["podman", "run", "--rm"]
    assert "-it" not in argv
    assert ["--name", "task-42-setup"] == argv[argv.index("--name"): argv.index("--name") + 2]
    assert f"{wt}:{wt}" in argv and f"{clone}:{clone}" in argv
    assert "agent-ops-npm-cache:/root/.npm" in argv
    assert "agent-ops-xdg-cache:/root/.cache" in argv
    assert ["-w", wt] == argv[argv.index("-w"): argv.index("-w") + 2]
    # no session-only mounts, no resource caps
    joined = " ".join(argv)
    assert "claude-home" not in joined and ".config/gh" not in joined
    assert "--memory" not in argv and "--cpus" not in argv
    assert argv[-2:] == ["agent-ops-session", "scripts/provision-worktree.sh"]


def test_setup_cmd_splits_multiword_commands(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    argv = containers.setup_cmd("task-7-setup", wt, "bash scripts/provision.sh --fast")
    assert argv[-3:] == ["bash", "scripts/provision.sh", "--fast"]
