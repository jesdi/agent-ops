"""Private-state overlap checks must follow the launched session's state."""
import pytest

from dispatcher.sessions import Sessions
from tests.runtime_listener import launch_listener  # noqa: F401
from tests.test_sessions import herdr_fake_creating
from tests.test_session_state_authority import command_options, ordinary_worktree


pytestmark = pytest.mark.usefixtures("launch_listener")


@pytest.mark.parametrize("runtime,model", [
    ("claude", "anthropic/claude-opus-5"),
    ("codex", "openai/gpt-6.1-sol"),
])
def test_launch_rejects_explicit_private_state_inside_mounted_clone(
    tmp_path, monkeypatch, runtime, model,
):
    worktree = ordinary_worktree(tmp_path)
    explicit_state = tmp_path / "clone" / "private-state"
    environment_state = tmp_path / "unmounted-environment-state"
    explicit_state.mkdir()
    environment_state.mkdir()
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(environment_state))
    calls = []
    herdr_fake_creating(monkeypatch, calls)

    with pytest.raises(ValueError, match="overlaps private runtime state"):
        Sessions(state_dir=explicit_state).spawn_stage(
            "acme", 42, str(worktree), "P", "review", model, effort="high")

    assert not any(call[:2] == ["pane", "run"] for call in calls)


@pytest.mark.parametrize("runtime,model", [
    ("claude", "anthropic/claude-opus-5"),
    ("codex", "openai/gpt-6.1-sol"),
])
def test_launch_ignores_overlapping_environment_state_when_explicit_state_is_safe(
    tmp_path, monkeypatch, runtime, model,
):
    worktree = ordinary_worktree(tmp_path)
    explicit_state = tmp_path / "unmounted-explicit-state"
    environment_state = tmp_path / "clone" / "environment-state"
    explicit_state.mkdir()
    environment_state.mkdir()
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(environment_state))
    calls = []
    herdr_fake_creating(monkeypatch, calls)
    session = Sessions(state_dir=explicit_state)

    try:
        session.spawn_stage("acme", 42, str(worktree), "P", "review", model,
                            effort="high")
    except ValueError as error:
        pytest.fail(f"safe explicit state rejected due to environment-state overlap: {error}")

    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    _, environment, mounts = command_options(command)
    assert environment["AGENT_OPS_STATE_DIR"] == str(explicit_state)
    assert session.runtime_view("acme", 42)["binding"]["runtime"] == runtime
    destination = "/root/.claude" if runtime == "claude" else "/root/.codex"
    assert any(source == str(explicit_state / f"{runtime}-home") and target == destination
               for source, target, _ in mounts)
    wait = str(explicit_state / "wait")
    assert any(source == wait and target == wait and "ro" in options
               for source, target, options in mounts)
