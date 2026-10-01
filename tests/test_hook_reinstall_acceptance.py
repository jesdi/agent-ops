"""Locked, black-box acceptance tests for ticket 04: every launch and resume
reinstalls the Stop hook. See specs/stop-hook-background-work/ (requirement 5).
Observed only through the worktree's .agent/stop-hook.sh and
.claude/settings.local.json, driven via Sessions.spawn_stage / .resume."""
import json
import os
from pathlib import Path

import pytest

import dispatcher.workspace as workspace
from dispatcher import herdr
from dispatcher.sessions import Sessions
from tests.test_containers import make_worktree
from tests.test_workspace import target

REPO_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "stop-hook.sh"
STOP = {"hooks": {"Stop": [{"hooks": [{
    "type": "command",
    "command": "$CLAUDE_PROJECT_DIR/.agent/stop-hook.sh"}]}]}}


class FakeTab:
    def __init__(self, wt, seen):
        self.wt, self.seen = wt, seen

    def run(self, cmd):
        self.seen.append(snapshot(self.wt))
        return True


def snapshot(wt):
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    settings = Path(wt) / ".claude" / "settings.local.json"
    return {
        "hook": hook.read_text() if hook.exists() else None,
        "exec": hook.exists() and bool(hook.stat().st_mode & 0o111),
        "settings": settings.read_text() if settings.exists() else None,
    }


@pytest.fixture
def wt_seen(tmp_path, monkeypatch):
    """A worktree plus a list of file snapshots taken at launch-command time."""
    wt, _ = make_worktree(tmp_path)
    seen = []
    monkeypatch.setattr(herdr.Tab, "ensure",
                        lambda *a, **k: FakeTab(wt, seen))
    return wt, seen


def spawn(wt, model="claude-fable-5"):
    Sessions().spawn_stage("acme", 42, wt, "P", "plan", model)


def resume(wt, model="claude-fable-5"):
    Sessions().resume("acme", 42, wt, "go", model)


def launches():
    return [pytest.param(spawn, id="spawn_stage"), pytest.param(resume, id="resume")]


def stale_hook(wt, text="#!/bin/sh\nexit 1\n"):
    (Path(wt) / ".agent").mkdir(exist_ok=True)
    (Path(wt) / ".agent" / "stop-hook.sh").write_text(text)


def test_resume_replaces_stale_hook_with_current_executable_one(wt_seen):
    wt, seen = wt_seen
    stale_hook(wt)
    resume(wt)
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    assert hook.read_text() == REPO_HOOK.read_text()
    assert os.access(hook, os.X_OK)


def test_spawn_stage_restores_deleted_hook(wt_seen):
    wt, seen = wt_seen
    stale_hook(wt)
    (Path(wt) / ".agent" / "stop-hook.sh").unlink()
    spawn(wt)
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    assert hook.read_text() == REPO_HOOK.read_text()
    assert os.access(hook, os.X_OK)


@pytest.mark.parametrize("launch", launches())
def test_launch_installs_stop_hook_setting(wt_seen, launch):
    wt, seen = wt_seen
    launch(wt)
    settings = json.loads((Path(wt) / ".claude" / "settings.local.json").read_text())
    assert settings["hooks"] == STOP["hooks"]


@pytest.mark.parametrize("launch", launches())
def test_launch_keeps_other_settings_keys(wt_seen, launch):
    wt, seen = wt_seen
    (Path(wt) / ".claude").mkdir()
    perms = {"allow": ["Bash(ls:*)"], "deny": []}
    (Path(wt) / ".claude" / "settings.local.json").write_text(json.dumps(
        {"permissions": perms, "hooks": {"Stop": []}}))
    launch(wt)
    settings = json.loads((Path(wt) / ".claude" / "settings.local.json").read_text())
    assert settings["permissions"] == perms
    assert settings["hooks"] == STOP["hooks"]


@pytest.mark.parametrize("launch", launches())
@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]"],
                         ids=["missing", "invalid", "not-an-object"])
def test_missing_or_invalid_settings_replaced_and_launch_happens(wt_seen, launch, content):
    wt, seen = wt_seen
    if content is not None:
        (Path(wt) / ".claude").mkdir()
        (Path(wt) / ".claude" / "settings.local.json").write_text(content)
    launch(wt)
    assert len(seen) == 1, "launch command was never sent"
    settings = json.loads((Path(wt) / ".claude" / "settings.local.json").read_text())
    assert settings == STOP


@pytest.mark.parametrize("launch", launches())
def test_hook_and_setting_in_place_when_launch_command_is_sent(wt_seen, launch):
    wt, seen = wt_seen
    stale_hook(wt)
    launch(wt)
    assert len(seen) == 1
    at_launch = seen[0]
    assert at_launch["hook"] == REPO_HOOK.read_text() and at_launch["exec"]
    assert json.loads(at_launch["settings"]) == STOP


@pytest.mark.parametrize("op", ["spawn_stage", "resume"])
def test_dry_run_writes_nothing(tmp_path, wt_seen, op):
    wt, seen = wt_seen
    stale_hook(wt)
    before = snapshot(wt)
    s = Sessions(dry_run=True)
    if op == "spawn_stage":
        s.spawn_stage("acme", 42, wt, "P", "plan", "claude-fable-5")
    else:
        s.resume("acme", 42, wt, "go", "claude-fable-5")
    assert snapshot(wt) == before
    assert not (Path(wt) / ".claude").exists()
    assert not seen
    assert sorted(p.name for p in (Path(wt) / ".agent").iterdir()) == ["stop-hook.sh"]


def test_create_workspace_matches_a_launched_worktree(tmp_path, monkeypatch, wt_seen):
    wt, seen = wt_seen
    spawn(wt)
    launched = snapshot(wt)

    def fake_sh(args, cwd, timeout=300, log=None):
        if "worktree" in args:
            w = Path(args[-2])
            w.mkdir(parents=True, exist_ok=True)
            (w / ".git").write_text(f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    created = snapshot(workspace.create_workspace(target(tmp_path), 42))
    assert launched["hook"] == REPO_HOOK.read_text()
    assert launched["exec"]
    assert launched["settings"] is not None
    assert json.loads(created["settings"]) == json.loads(launched["settings"])
    assert created["hook"] == launched["hook"] and created["exec"]


@pytest.mark.parametrize("launch", launches())
def test_codex_launch_leaves_current_hook(wt_seen, launch):
    wt, seen = wt_seen
    stale_hook(wt)
    launch(wt, model="openai/gpt-5-codex")
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    assert hook.read_text() == REPO_HOOK.read_text()
    assert os.access(hook, os.X_OK)
    assert seen and seen[0]["hook"] == REPO_HOOK.read_text()
