"""Validity tests for the claude-home seed (ADR 0003 §1)."""
import json
import re
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "provision" / "claude-home"


def settings():
    return json.loads((SEED / "settings.json").read_text())


def test_seed_tree_complete():
    assert (SEED / "settings.json").is_file()
    assert (SEED / "CLAUDE.md").is_file()
    assert (SEED / "skills").is_dir()
    assert (SEED / "hooks" / "block-dangerous-git.sh").is_file()


def test_enabled_plugins_values_are_latest_or_pinned():
    plugins = settings()["enabledPlugins"]
    assert plugins, "seed must declare at least one plugin"
    for pid, want in plugins.items():
        assert "@" in pid, f"{pid}: must be plugin@marketplace"
        assert want is True or re.fullmatch(r"\d+\.\d+\.\d+", want), \
            f"{pid}: value must be true (latest) or 'x.y.z' (pinned)"


def test_superpowers_is_gone_and_frontend_design_stays():
    plugins = settings()["enabledPlugins"]
    assert "superpowers@claude-plugins-official" not in plugins
    assert "frontend-design@claude-plugins-official" in plugins


def test_vendored_skills_are_complete_and_listed():
    skills = SEED / "skills"
    dirs = sorted(p.name for p in skills.iterdir() if p.is_dir())
    listed = {}
    if (skills / "VENDORED.json").is_file():
        listed = json.loads((skills / "VENDORED.json").read_text())["skills"]
    for name in dirs:
        assert (skills / name / "SKILL.md").is_file(), f"{name}: no SKILL.md"
        assert name in listed, f"{name}: not recorded in VENDORED.json — run make vendor-skills"
    assert set(listed) == set(dirs), "VENDORED.json lists a skill that is not on disk"


def test_claude_md_names_the_lease_push_and_ticket_discipline():
    text = (SEED / "CLAUDE.md").read_text()
    assert "--force-with-lease" in text
    assert ".agent/" in text and "stage.json" in text


def test_no_memory_plugins():
    # ADR 0003 §5: the box is memoryless — no engram, no MCP memory.
    assert not any("engram" in pid for pid in settings()["enabledPlugins"])
    assert "mcpServers" not in settings()


def test_hook_registration_points_at_seed_hook():
    hooks = settings()["hooks"]["PreToolUse"]
    commands = [h["command"] for entry in hooks for h in entry["hooks"]]
    assert commands == ["~/.claude/hooks/block-dangerous-git.sh"]
    # Every registered hook must exist in the seed (delete-propagation
    # means claude-home has nothing else).
    for c in commands:
        assert (SEED / "hooks" / Path(c).name).is_file()


def test_claude_md_box_conventions():
    text = (SEED / "CLAUDE.md").read_text()
    assert "co-author" in text.lower()
    assert "TDD" in text
