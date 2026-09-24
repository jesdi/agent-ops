"""Locked, black-box acceptance tests for ticket 08: stage sessions run on
Codex. See .agent/tickets/08-codex-stage-sessions.md and
docs/specs/2026-09-24-codex-runtime-design.md (Runtimes, Launcher, Prompts,
Console). Exercises only the public seam: the session commands built for an
openai/... entry (spawn and resume) and the console's task view. Skips "On
the box" (manual)."""
from pathlib import Path

import pytest

from dispatcher import containers, sessions
from dispatcher.prompts import render_stage_prompt
from dispatcher.state import Stage
from tests.test_containers import make_worktree
from tests.test_prompts import CTX


def _openai_runtime():
    """The runtime the spec assigns 'openai'. Raises until RUNTIMES carries
    an 'openai' entry — that failure (not an ImportError) is the missing
    behaviour this file locks in."""
    from dispatcher.runtimes import runtime_for
    return runtime_for("openai/gpt-5-codex")


# --- 1. spawn command for an openai/... entry -------------------------------

def test_codex_spawn_mounts_host_binary_and_codex_home(tmp_path, monkeypatch):
    # Mirrors tests/test_containers.py::test_containers_run_the_hosts_claude_read_only
    # — the host's native codex binary, resolved through ~/.local/bin the
    # same way the host's claude binary is.
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    real_codex = home / "opt" / "codex-1.2.3"
    real_codex.parent.mkdir(parents=True)
    real_codex.write_text("")
    (home / ".local" / "bin" / "codex").symlink_to(real_codex)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    runtime = _openai_runtime()
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "gpt-5-codex", "P",
                                 effort="high", runtime=runtime)

    import os as _os
    mount = f"{_os.path.realpath(real_codex)}:/usr/local/bin/codex:ro"
    assert f"-v {mount}" in cmd

    # codex-home mount + CODEX_HOME, no claude-home, no CLAUDE_*.
    assert f"-v {tmp_path / 'state'}/codex-home:/root/.codex" in cmd
    assert "-e CODEX_HOME=/root/.codex" in cmd
    assert "claude-home" not in cmd
    assert "CLAUDE_CONFIG_DIR" not in cmd
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in cmd


def test_codex_spawn_command_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    runtime = _openai_runtime()
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "gpt-5-codex", "P",
                                 effort="high", runtime=runtime)

    assert "--model gpt-5-codex" in cmd
    assert "-c model_reasoning_effort=high" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert f"-c 'notify=[\"{wt}/.agent/stop-hook.sh\"]'" in cmd
    assert f"-c 'projects.\"{wt}\".trust_level=\"trusted\"'" in cmd
    assert "--remote-control" not in cmd


def test_codex_spawn_omits_effort_flag_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    runtime = _openai_runtime()
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "gpt-5-codex", "P",
                                 effort="", runtime=runtime)
    assert "model_reasoning_effort" not in cmd
    assert "--model gpt-5-codex" in cmd


# --- 2. resume command for an openai entry -----------------------------------

def test_codex_resume_ends_with_resume_last_message():
    runtime = _openai_runtime()
    assert runtime.resume("'hi'") == "resume --last 'hi'"


def test_codex_session_resume_tab_gets_herdr_agent_codex(tmp_path, monkeypatch):
    from dispatcher import herdr
    wt, clone = make_worktree(tmp_path)
    (Path(wt) / ".git").write_text(f"gitdir: {clone}/.git/worktrees/task-42\n")
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    calls = []

    def fake_ensure(*args, **kwargs):
        calls.append(kwargs)
        return None  # sessions._launch raises when Tab.ensure returns None;
                     # that's fine, we only need to see the call it made.

    monkeypatch.setattr(herdr.Tab, "ensure", fake_ensure)
    with pytest.raises(RuntimeError):
        sessions.Sessions().resume("acme", 42, wt, "go", "openai/gpt-5-codex")
    assert calls, "Tab.ensure was never called"
    assert calls[0].get("env") == {"HERDR_AGENT": "codex"}


# --- 3. Claude commands stay byte-identical: regression guard ---------------

def test_claude_session_never_mounts_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/home/agent/agent-ops-state")
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    wt, clone = make_worktree(tmp_path)
    cmd = containers.session_cmd("task-42", wt, "2g", "2", "claude-fable-5", "P")
    assert "codex-home" not in cmd
    assert "CODEX_HOME" not in cmd
    assert "/usr/local/bin/codex" not in cmd


# --- 4. plan prompt's reviewer step reads correctly without subagents ------

def test_plan_prompt_has_a_fallback_for_runtimes_without_subagents():
    out = render_stage_prompt(Stage.PLAN, CTX)
    assert ("Dispatch four reviewer subagents if your runtime supports them; "
            "otherwise run the four reviews yourself, one after another, "
            "each a fresh pass over the spec and the tickets.") in out
    # the old, no-fallback sentence must be gone
    assert "Dispatch four reviewer subagents over the spec and the ticket set" not in out
