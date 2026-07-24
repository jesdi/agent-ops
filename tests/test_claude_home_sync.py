"""E2E tests for provision/claude-home-sync.sh managed-path convergence
(ADR 0003 §2): full authority inside settings.json/CLAUDE.md/skills/hooks,
delete-propagation included; machine state never touched."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "provision" / "claude-home-sync.sh"
REAL_SEED = Path(__file__).resolve().parent.parent / "provision" / "claude-home"


@pytest.fixture
def rig(tmp_path):
    # A repo dir holding a copy of the real seed, so tests exercise the
    # actual shipped config; individual tests mutate their copy.
    repo = tmp_path / "agent-ops"
    seed = repo / "provision" / "claude-home"
    seed.parent.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(REAL_SEED), str(seed)], check=True)

    state = tmp_path / "state"
    home = state / "claude-home"
    env = dict(
        os.environ,
        AGENT_OPS_REPO=str(repo),
        AGENT_OPS_STATE_DIR=str(state),
    )
    return SimpleNamespace(repo=repo, seed=seed, state=state, home=home, env=env)


def run_sync(rig, **env_extra):
    return subprocess.run(["bash", str(SCRIPT)], env={**rig.env, **env_extra},
                          capture_output=True, text=True)


def test_fresh_home_materialized(rig):
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert (rig.home / "CLAUDE.md").read_text() == \
        (rig.seed / "CLAUDE.md").read_text()
    assert (rig.home / "hooks" / "block-dangerous-git.sh").stat().st_mode & 0o111
    assert (rig.home / "skills").is_dir()


def test_settings_normalized_pins_to_true(rig):
    s = json.loads((rig.seed / "settings.json").read_text())
    s["enabledPlugins"]["pinned@claude-plugins-official"] = "1.2.3"
    (rig.seed / "settings.json").write_text(json.dumps(s))
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    out = json.loads((rig.home / "settings.json").read_text())
    # Runtime copy: every declaration coerced to boolean true.
    assert all(v is True for v in out["enabledPlugins"].values())
    assert set(out["enabledPlugins"]) == set(s["enabledPlugins"])
    assert out["hooks"] == s["hooks"]


def test_drift_healed_and_deletes_propagate(rig):
    run_sync(rig)
    (rig.home / "CLAUDE.md").write_text("# drifted\n")
    (rig.home / "skills" / "stale-skill").mkdir(parents=True)
    (rig.home / "skills" / "stale-skill" / "SKILL.md").write_text("stale")
    (rig.home / "hooks" / "stale-hook.sh").write_text("stale")
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert "drifted" not in (rig.home / "CLAUDE.md").read_text()
    assert not (rig.home / "skills" / "stale-skill").exists()
    assert not (rig.home / "hooks" / "stale-hook.sh").exists()


def test_seed_skill_removal_propagates(rig):
    skill = rig.seed / "skills" / "temp-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("v1")
    run_sync(rig)
    assert (rig.home / "skills" / "temp-skill" / "SKILL.md").exists()
    subprocess.run(["rm", "-r", str(skill)], check=True)
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert not (rig.home / "skills" / "temp-skill").exists()


def test_machine_state_never_touched(rig):
    # ADR 0003 §2: credentials, transcripts, plugin cache are machine state.
    (rig.home / "projects" / "p").mkdir(parents=True)
    (rig.home / "projects" / "p" / "t.jsonl").write_text("{}\n")
    (rig.home / "plugins" / "cache").mkdir(parents=True)
    (rig.home / "plugins" / "cache" / "blob").write_text("cache")
    (rig.home / ".credentials.json").write_text('{"secret": 1}')
    (rig.home / "todos.json").write_text("[]")
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert (rig.home / "projects" / "p" / "t.jsonl").read_text() == "{}\n"
    assert (rig.home / "plugins" / "cache" / "blob").read_text() == "cache"
    assert (rig.home / ".credentials.json").read_text() == '{"secret": 1}'
    assert (rig.home / "todos.json").read_text() == "[]"


def test_respects_claude_home_override(rig):
    other = rig.state / "elsewhere"
    r = run_sync(rig, AGENT_OPS_CLAUDE_HOME=str(other))
    assert r.returncode == 0, r.stderr
    assert (other / "CLAUDE.md").exists()
    assert not (rig.home / "CLAUDE.md").exists()
