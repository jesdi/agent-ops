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
    assert plugins, "seed must declare at least the superpowers plugin"
    for pid, want in plugins.items():
        assert "@" in pid, f"{pid}: must be plugin@marketplace"
        assert want is True or re.fullmatch(r"\d+\.\d+\.\d+", want), \
            f"{pid}: value must be true (latest) or 'x.y.z' (pinned)"


def test_superpowers_declared():
    assert "superpowers@claude-plugins-official" in settings()["enabledPlugins"]


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
