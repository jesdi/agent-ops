"""Public launch acceptance: explicit host state and canonical bind sources."""
from pathlib import Path
import shlex

import pytest

from dispatcher.models import Entry
from dispatcher.sessions import Sessions, podman_cmd
from tests.runtime_listener import launch_listener, seed_resume_task  # noqa: F401
from tests.test_sessions import (
    LIVE,
    assert_codex_supervisor_prompt,
    herdr_fake,
    herdr_fake_creating,
)


pytestmark = pytest.mark.usefixtures("launch_listener")


def ordinary_worktree(root, *, relative_gitdir=False):
    worktree = root / "worktree"
    worktree.mkdir()
    gitdir = root / "clone" / ".git" / "worktrees" / "task-42"
    gitdir.mkdir(parents=True)
    value = str(Path("..") / gitdir.relative_to(root)) if relative_gitdir else str(gitdir)
    (worktree / ".git").write_text(f"gitdir: {value}\n")
    return worktree


def command_options(command):
    """Read effective Podman options from the command actually sent to herdr."""
    words = shlex.split(command)
    environment = {}
    mounts = []
    for index, word in enumerate(words[:-1]):
        if word in {"-e", "--env"}:
            key, separator, value = words[index + 1].partition("=")
            if separator:
                environment[key] = value
        if word in {"-v", "--volume"}:
            source, destination, *options = words[index + 1].split(":")
            mounts.append((source, destination, options))
    return words, environment, mounts


def main_cli_words(words):
    for index, word in enumerate(words[:-2]):
        if Path(word).name in {"sh", "bash"} and words[index + 1] in {"-c", "-lc"}:
            return shlex.split(words[index + 2])
    return words


def cli_option(words, name):
    for index, word in enumerate(words):
        if word.startswith(name + "="):
            return word[len(name) + 1:]
        if word == name and index + 1 < len(words):
            return words[index + 1]
    return None


@pytest.mark.parametrize("runtime,model,effort", [
    ("claude", "anthropic/claude-opus-5", "high"),
    ("codex", "openai/gpt-6.1-sol", "high"),
])
@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize("secondary", [False, True], ids=["primary", "secondary"])
def test_sessions_explicit_state_controls_launched_client_and_homes(
    tmp_path, monkeypatch, runtime, model, effort, resume, secondary,
):
    environment_state = tmp_path / "environment-state"
    environment_state.mkdir()
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(environment_state))
    worktree = ordinary_worktree(tmp_path)
    calls = []
    session = Sessions(state_dir=tmp_path)
    second = None
    if secondary:
        second = (Entry("openai", "gpt-6-astra", "high") if runtime == "claude"
                  else Entry("anthropic", "claude-opus-5", "high"))
    prompt = 'literal prompt: "quotes", $(do-not-expand), `literal`'
    conversation = "--recorded session; $(literal)"
    if resume:
        seed_resume_task(tmp_path, str(worktree))
        herdr_fake(monkeypatch, LIVE + [(("pane", "run"), 0, "")], calls)
        session.resume("acme", 42, str(worktree), prompt, model, effort,
                       second=second, session_id=conversation)
    else:
        herdr_fake_creating(monkeypatch, calls)
        session.spawn_stage("acme", 42, str(worktree), prompt, "review", model,
                            effort, second=second)

    command = next(call[3] for call in calls if call[:2] == ["pane", "run"])
    words, environment, mounts = command_options(command)
    view = session.runtime_view("acme", 42)
    assert view["binding"]["worktree"] == str(worktree)
    assert view["binding"]["runtime"] == runtime
    assert view["binding"]["stage"] == "review"
    if resume:
        assert view["binding"]["conversation_id"] == conversation
    assert environment["AGENT_OPS_LAUNCH_ID"] == view["binding"]["launch_id"]
    if runtime == "codex":
        assert_codex_supervisor_prompt(command, str(worktree), prompt, model,
                                       effort=effort,
                                       session_id=conversation if resume else None)
    else:
        cli = main_cli_words(words)
        assert cli_option(cli, "--model") == "claude-opus-5"
        assert cli_option(cli, "--effort") == effort
        if resume:
            assert cli_option(cli, "--resume") == conversation
            assert cli[-1] == prompt
        else:
            assert cli_option(cli, "--session-id") == view["binding"]["conversation_id"]
            assert (worktree / ".agent" / "prompt-review.md").read_text() == prompt

    problems = []
    if environment.get("AGENT_OPS_STATE_DIR") != str(tmp_path):
        problems.append(f"client state selected {environment.get('AGENT_OPS_STATE_DIR')!r}")
    wait = str(tmp_path / "wait")
    if not any(source == wait and destination == wait and "ro" in options
               for source, destination, options in mounts):
        problems.append("explicit state's listener directory is not mounted read-only")
    homes = {runtime}
    if secondary:
        homes.add("codex" if runtime == "claude" else "claude")
    for home in homes:
        destination = "/root/.codex" if home == "codex" else "/root/.claude"
        expected = str(tmp_path / f"{home}-home")
        if not any(source == expected and target == destination
                   for source, target, _ in mounts):
            problems.append(f"{home} home does not come from explicit state")
    if any(Path(source).is_relative_to(environment_state)
           for source, _, _ in mounts):
        problems.append("a bind source comes from the conflicting environment state")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("relative_worktree,relative_gitdir,relative_state", [
    (True, False, False),
    (False, True, False),
    (False, False, True),
    (True, True, True),
], ids=["worktree", "clone", "state", "all"])
def test_command_builder_relative_sources_keep_construction_cwd_identity(
    tmp_path, monkeypatch, relative_worktree, relative_gitdir, relative_state,
):
    construction = tmp_path / "construction"
    construction.mkdir()
    terminal = tmp_path / "later-terminal"
    terminal.mkdir()
    monkeypatch.chdir(construction)
    worktree = ordinary_worktree(construction, relative_gitdir=relative_gitdir)
    state = construction / "private-state"
    state.mkdir()
    monkeypatch.setenv("AGENT_OPS_STATE_DIR",
                       "private-state" if relative_state else str(state))
    selected_worktree = "worktree" if relative_worktree else str(worktree)
    command = podman_cmd("acme", 42, selected_worktree, "2g", "2",
                         "claude-opus-5", "P")
    monkeypatch.chdir(terminal)
    words, _, mounts = command_options(command)
    for expected in (worktree, construction / "clone", state / "claude-home", state / "wait"):
        assert any(source == str(expected.resolve()) for source, _, _ in mounts), (
            f"bind source must keep the validated absolute host identity: {expected}")
    assert all(Path(source).is_absolute() for source, _, _ in mounts), (
        "relative bind sources can become named volumes or resolve under the later terminal cwd")
    assert words[words.index("-w") + 1] == str(worktree.resolve())


def test_direct_command_builder_without_explicit_state_keeps_environment_choice(
    tmp_path, monkeypatch,
):
    state = tmp_path / "direct-builder-state"
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
    worktree = ordinary_worktree(tmp_path)
    command = podman_cmd("acme", 42, str(worktree), "2g", "2", "claude-opus-5", "P")
    _, environment, mounts = command_options(command)
    assert environment["AGENT_OPS_STATE_DIR"] == str(state)
    assert (str(state / "claude-home"), "/root/.claude", []) in mounts
    assert any(source == str(state / "wait") and "ro" in options
               for source, _, options in mounts)
