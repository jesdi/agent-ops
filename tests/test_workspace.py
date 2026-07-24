import json
from pathlib import Path

import dispatcher.workspace as workspace
from dispatcher.config import Target


def target(tmp_path: Path) -> Target:
    return Target(
        name="portfolio_eval", repo="jesdi/portfolio_eval",
        clone_path=str(tmp_path / "repo"),
        worktrees_path=str(tmp_path / "repo.worktrees"),
        rank_cmd="rank", setup_cmd="scripts/setup-worktree.sh",
        verify_cmd="make e2e-slot SLOT={slot}",
        project_number=1, project_owner="jesdi",
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="I",
    )


def test_create_workspace(tmp_path: Path, monkeypatch):
    calls = []

    def fake_sh(args, cwd, timeout=300):
        calls.append((args, cwd, timeout))
        if "worktree" in args:  # simulate git creating the dir
            wt = Path(args[-2])
            wt.mkdir(parents=True, exist_ok=True)
            (wt / ".git").write_text(
                f"gitdir: {tmp_path / 'repo'}/.git/worktrees/task-42\n")

    monkeypatch.setattr(workspace, "_sh", fake_sh)
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    t = target(tmp_path)
    wt = workspace.create_workspace(t, 42)

    assert wt == str(tmp_path / "repo.worktrees" / "task-42")
    fetch = calls[0]
    assert fetch[:2] == (["git", "fetch", "origin"], t.clone_path)
    add = calls[1]
    assert add[0][:3] == ["git", "worktree", "add"]
    assert "agent/task-42" in add[0] and add[1] == t.clone_path
    setup_args, setup_cwd, setup_timeout = calls[2]
    assert setup_args[:3] == ["podman", "run", "--rm"]
    assert "task-42-setup" in setup_args
    assert f"{wt}:{wt}" in setup_args
    assert f"{t.clone_path}:{t.clone_path}" in setup_args
    assert "agent-ops-npm-cache:/root/.npm" in setup_args
    assert setup_args[-1] == "scripts/setup-worktree.sh"
    assert setup_cwd == wt
    assert setup_timeout == 1800

    assert json.loads((Path(wt) / ".agent" / "task.json").read_text()) == {"issue": 42}
    hook = Path(wt) / ".agent" / "stop-hook.sh"
    assert hook.exists() and hook.stat().st_mode & 0o111
    settings = json.loads((Path(wt) / ".claude" / "settings.local.json").read_text())
    stop = settings["hooks"]["Stop"][0]["hooks"][0]
    assert stop["type"] == "command" and ".agent/stop-hook.sh" in stop["command"]


def test_dry_run_creates_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(workspace, "_sh",
                        lambda a, cwd, timeout=300: (_ for _ in ()).throw(AssertionError))
    wt = workspace.create_workspace(target(tmp_path), 42, dry_run=True)
    assert not Path(wt).exists()
