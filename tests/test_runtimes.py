import pytest

from dispatcher import runtimes


def test_bare_and_anthropic_ids_run_on_claude():
    assert runtimes.runtime_for("claude-fable-5") is runtimes.CLAUDE
    assert runtimes.runtime_for("anthropic/claude-opus-5") is runtimes.CLAUDE


def test_openai_ids_run_on_codex():
    assert runtimes.runtime_for("openai/gpt-5-codex") is runtimes.CODEX


def test_a_provider_with_no_runtime_is_a_clear_error():
    with pytest.raises(ValueError, match="no runtime for provider 'mistral'"):
        runtimes.runtime_for("mistral/large")


def test_claude_builds_its_launch_and_resume_args():
    assert runtimes.CLAUDE.launch("task-42", "/wt", "claude-fable-5", "high") == (
        "claude --remote-control task-42 --permission-mode auto "
        "--model claude-fable-5 --effort high")
    assert runtimes.CLAUDE.resume("'hi'") == "--continue 'hi'"


def test_a_session_on_a_provider_with_no_runtime_fails_before_any_tab(tmp_path, monkeypatch):
    from dispatcher import herdr, sessions
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path}/clone/.git/worktrees/t\n")
    tabs = []
    monkeypatch.setattr(herdr.Tab, "ensure", lambda *a, **k: tabs.append(a))
    with pytest.raises(ValueError, match="no runtime for provider 'mistral'"):
        sessions.Sessions().resume("acme", 42, str(tmp_path), "go", "mistral/large")
    assert tabs == []


def test_every_configurable_provider_has_a_runtime_and_vice_versa():
    from dispatcher.models import PROVIDER_EFFORTS
    assert runtimes.RUNTIMES.keys() == PROVIDER_EFFORTS.keys()
    with pytest.raises(RuntimeError, match="providers differ"):
        runtimes.check_providers(runtimes.RUNTIMES,
                                 {**PROVIDER_EFFORTS, "mistral": ("low",)})


def test_the_cli_name_derives_the_binary_its_mount_and_the_resume_line():
    assert runtimes.CODEX.binary == ".local/bin/codex"
    assert runtimes.CODEX.package == "/opt/codex"
    assert runtimes.CLAUDE.package == ""
    assert runtimes.CLAUDE.resume_cmd() == "claude --continue"
    assert runtimes.CODEX.resume_cmd("'hi'") == "codex resume --last 'hi'"
