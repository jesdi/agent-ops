"""infra_config.add_project_text: the onboarding's edit of infra's
targets.yaml keeps every other byte, comments and layout included."""
from pathlib import Path

import pytest
import yaml

from dispatcher.infra_config import add_project_text

EXAMPLE = (Path(__file__).parent.parent / "targets.example.yaml").read_text()

BLOCK = """\
- name: analytics
  paused: true
  repositories:
  - name: analytics
"""


def test_the_block_lands_after_the_last_project_indented_like_it():
    out = add_project_text(EXAMPLE, BLOCK)
    indented = "".join(f"  {line}" for line in BLOCK.splitlines(keepends=True))
    assert out == EXAMPLE + indented
    assert yaml.safe_load(out)["projects"][-1]["name"] == "analytics"


def test_keys_and_comments_after_the_list_stay_after_it():
    text = ("state_dir: /s\n"
            "projects:  # every project\n"
            "- name: a\n"
            "  board: {owner: o, number: 1}\n"
            "\n"
            "  # a comment inside the last project\n"
            "  paused: true\n"
            "\n"
            "# about capacity\n"
            "capacity: 2")
    out = add_project_text(text, BLOCK)
    assert out == text.replace("  paused: true\n", "  paused: true\n" + BLOCK, 1)
    assert [p["name"] for p in yaml.safe_load(out)["projects"]] == ["a", "analytics"]


def test_a_list_that_ends_the_file_without_a_newline_gets_one():
    out = add_project_text("projects:\n  - name: a", BLOCK)
    assert out.startswith("projects:\n  - name: a\n  - name: analytics\n")


def test_a_file_without_a_block_projects_list_is_refused():
    with pytest.raises(ValueError, match="projects:"):
        add_project_text("state_dir: /s\nprojects: []\n", BLOCK)
