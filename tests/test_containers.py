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
        "claude --remote-control task-42 --permission-mode auto "
        "--model claude-fable-5 --continue 'hi'")


def test_session_cmd_enables_remote_control_named_after_the_task(tmp_path: Path, monkeypatch):
    # Every interactive box session opts into Remote Control so it is
    # reachable/controllable from claude.ai and the Claude app, named after
    # its task (the container/session name) to be identifiable there.
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5", "P")
    assert "--remote-control task-42" in cmd


def test_session_cmd_sets_claude_config_dir(tmp_path: Path, monkeypatch):
    # Claude Code keeps onboarding/trust state in ~/.claude.json — a SIBLING
    # of ~/.claude, so it falls outside the claude-home mount and every
    # container boots as a fresh install (first-run wizard, login screen,
    # trust dialog — task #192 sat on the theme picker for 1.5h).
    # CLAUDE_CONFIG_DIR relocates all of it inside the mounted claude-home.
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5", "P")
    assert "-e CLAUDE_CONFIG_DIR=/root/.claude" in cmd


def test_session_cmd_exposes_waitd_socket(tmp_path: Path, monkeypatch):
    # The Stop hook runs INSIDE the container; without the wait-dir mount
    # and AGENT_OPS_STATE_DIR it resolves the socket to a path that does
    # not exist there and every waiting ping is silently lost (task #187).
    # Only the wait dir is mounted — the state dir also holds op-token.env.
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5", "P")
    assert ("-v /home/agent/agent-ops-state/wait"
            ":/home/agent/agent-ops-state/wait" in cmd)
    assert "-e AGENT_OPS_STATE_DIR=/home/agent/agent-ops-state" in cmd


def test_session_cmd_creates_wait_dir_mount_source(tmp_path: Path, monkeypatch):
    # podman errors on a missing bind source. waitd creates the dir when it
    # (re)starts with the new path, but a dispatcher spawn can race a
    # not-yet-restarted waitd right after deploy — ensure the source here.
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, _ = make_worktree(tmp_path)
    containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5", "P")
    assert (tmp_path / "state" / "wait").is_dir()


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


def test_triage_cmd_read_only_clone_and_report_mount(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    from dispatcher import containers
    cmd = containers.triage_cmd(
        "triage-o-r", "/repos/r", "/state/triage", "1500m", "2",
        "claude-opus-4-8", "/triage/o-r-2026-07-30-prompt.md")
    assert cmd[:3] == ["podman", "run", "--rm"]
    assert "-it" not in cmd
    assert "/repos/r:/repos/r:ro" in cmd
    assert "/state/triage:/triage" in cmd
    assert f"{tmp_path / 'state'}/claude-home:/root/.claude" in cmd
    i = cmd.index("-w")
    assert cmd[i + 1] == "/repos/r"
    assert cmd[cmd.index("--memory") + 1] == "1500m"
    # The prompt is read from the /triage mount inside the container, never
    # passed as an argv string (128 KiB MAX_ARG_STRLEN), so the tail is a
    # shell line — the only place command substitution can happen.
    assert cmd[-3:-1] == ["bash", "-c"]
    assert cmd[-1] == ('claude -p "$(cat /triage/o-r-2026-07-30-prompt.md)" '
                       '--permission-mode auto --model claude-opus-4-8')


def test_triage_cmd_quotes_prompt_path_and_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    from dispatcher import containers
    cmd = containers.triage_cmd(
        "triage-o-r", "/repos/r", "/state/triage", "1500m", "2",
        "m; rm -rf /", "/triage/a b; rm -rf /.md")
    shell_line = cmd[-1]
    assert "'/triage/a b; rm -rf /.md'" in shell_line
    assert "'m; rm -rf /'" in shell_line
