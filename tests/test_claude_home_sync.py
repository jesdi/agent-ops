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

FAKE_CLAUDE = r'''#!/bin/sh
# Fake claude CLI: logs every call (with CLAUDE_CONFIG_DIR), serves `plugin
# list --json` from $AGENT_OPS_FAKE_PLUGIN_LIST, and mutates that file on
# install/uninstall so the sync script's verify step sees the effect.
# AGENT_OPS_FAKE_IGNORE_PIN=1 simulates a CLI that cannot pin: installs
# record version 9.9.9 regardless of a requested pin.
echo "claude $* CONFIG=${CLAUDE_CONFIG_DIR:-}" >> "$AGENT_OPS_CALLS_LOG"
case "$1 $2" in
  "plugin list") cat "$AGENT_OPS_FAKE_PLUGIN_LIST" ;;
  "plugin install") python3 - "$AGENT_OPS_FAKE_PLUGIN_LIST" "$3" <<'PYEOF'
import json, os, sys
path, arg = sys.argv[1], sys.argv[2]
parts = arg.split("@")
if len(parts) == 3 and os.environ.get("AGENT_OPS_FAKE_IGNORE_PIN") != "1":
    pid, version = "@".join(parts[:2]), parts[2]
else:
    pid, version = "@".join(parts[:2]), "9.9.9"
plugins = [p for p in json.load(open(path)) if p["id"] != pid]
plugins.append({"id": pid, "version": version})
json.dump(plugins, open(path, "w"))
PYEOF
  ;;
  "plugin uninstall") python3 - "$AGENT_OPS_FAKE_PLUGIN_LIST" "$3" <<'PYEOF'
import json, sys
path, pid = sys.argv[1], sys.argv[2]
json.dump([p for p in json.load(open(path)) if p["id"] != pid],
          open(path, "w"))
PYEOF
  ;;
esac
'''


@pytest.fixture
def rig(tmp_path):
    # A repo dir holding a copy of the real seed, so tests exercise the
    # actual shipped config; individual tests mutate their copy.
    repo = tmp_path / "agent-ops"
    seed = repo / "provision" / "claude-home"
    seed.parent.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(REAL_SEED), str(seed)], check=True)

    calls = tmp_path / "calls.log"
    plugin_list = tmp_path / "plugins.json"
    plugin_list.write_text(json.dumps(
        [{"id": "superpowers@claude-plugins-official", "version": "4.0.0"}]))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)

    state = tmp_path / "state"
    home = state / "claude-home"
    env = dict(
        os.environ,
        AGENT_OPS_REPO=str(repo),
        AGENT_OPS_STATE_DIR=str(state),
        AGENT_OPS_CLAUDE=str(claude),
        AGENT_OPS_CALLS_LOG=str(calls),
        AGENT_OPS_FAKE_PLUGIN_LIST=str(plugin_list),
    )
    return SimpleNamespace(repo=repo, seed=seed, state=state, home=home,
                           env=env, calls=calls, plugin_list=plugin_list)


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


# ---------------------------------------------------------------------------
# Plugin convergence tests (Task 4)
# ---------------------------------------------------------------------------

def calls(rig):
    return rig.calls.read_text() if rig.calls.exists() else ""


def declare(rig, plugins):
    s = json.loads((rig.seed / "settings.json").read_text())
    s["enabledPlugins"] = plugins
    (rig.seed / "settings.json").write_text(json.dumps(s))


def installed(rig):
    return {p["id"]: p["version"]
            for p in json.loads(rig.plugin_list.read_text())}


def test_missing_declared_plugin_installed_into_claude_home(rig):
    rig.plugin_list.write_text("[]")
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    log = calls(rig)
    assert "plugin install superpowers@claude-plugins-official" in log
    # Every claude call must target claude-home, not the agent user's ~/.claude.
    for line in log.splitlines():
        assert f"CONFIG={rig.home}" in line


def test_undeclared_plugin_uninstalled(rig):
    rig.plugin_list.write_text(json.dumps([
        {"id": "superpowers@claude-plugins-official", "version": "4.0.0"},
        {"id": "engram@engram", "version": "0.1.0"},
    ]))
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert "plugin uninstall engram@engram" in calls(rig)
    assert "engram@engram" not in installed(rig)


def test_pinned_plugin_installs_exact_version(rig):
    declare(rig, {"superpowers@claude-plugins-official": "5.0.0"})
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert "plugin install superpowers@claude-plugins-official@5.0.0" in calls(rig)
    assert installed(rig)["superpowers@claude-plugins-official"] == "5.0.0"


def test_pin_mismatch_fails_loudly(rig):
    declare(rig, {"superpowers@claude-plugins-official": "5.0.0"})
    r = run_sync(rig, AGENT_OPS_FAKE_IGNORE_PIN="1")
    assert r.returncode != 0
    assert "superpowers@claude-plugins-official" in r.stderr
    assert "5.0.0" in r.stderr


def test_satisfied_state_only_lists(rig):
    run_sync(rig)                       # first pass writes the stamp
    rig.calls.write_text("")
    r = run_sync(rig)                   # second pass: nothing to do
    assert r.returncode == 0, r.stderr
    assert calls(rig), "expected at least plugin list calls"
    for line in calls(rig).splitlines():
        assert "plugin list" in line, f"unexpected action: {line}"


def test_declaration_change_updates_latest_plugins(rig):
    run_sync(rig)                       # stamp written for current set
    declare(rig, {"superpowers@claude-plugins-official": True,
                  "extra@claude-plugins-official": True})
    rig.calls.write_text("")
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    log = calls(rig)
    assert "plugin install extra@claude-plugins-official" in log
    # superpowers tracks latest and the declared set changed → update it.
    assert "plugin update superpowers@claude-plugins-official" in log


def test_marketplace_added_before_first_install(rig):
    # A fresh claude-home has no marketplaces (the official one is only
    # auto-added by interactive first-run, which never happens on the box);
    # `plugin install name@marketplace` then fails with "not found in
    # marketplace". The sync must ensure the marketplace before installing.
    rig.plugin_list.write_text("[]")
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    lines = calls(rig).splitlines()
    adds = [i for i, l in enumerate(lines)
            if "plugin marketplace add anthropics/claude-plugins-official" in l]
    installs = [i for i, l in enumerate(lines) if "plugin install" in l]
    assert adds, "expected a marketplace add before installing"
    assert installs and adds[0] < installs[0]


def test_marketplace_untouched_when_nothing_to_install(rig):
    run_sync(rig)                       # converge + stamp
    rig.calls.write_text("")
    r = run_sync(rig)                   # steady state: list-only pass
    assert r.returncode == 0, r.stderr
    assert "plugin marketplace" not in calls(rig)


def test_stamp_written(rig):
    r = run_sync(rig)
    assert r.returncode == 0, r.stderr
    assert (rig.state / "claude-home-plugins.stamp").read_text().strip()
