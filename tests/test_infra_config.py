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


# A targets.yaml as a person keeps it: items indented two spaces under
# projects:, comments between and inside entries, quoted commands holding
# ":" and "#", values over several lines, a top-level key after the list and
# no newline at the end. HEAD runs to the last line of the last project.
HAND_HEAD = """\
# agent-ops targets, kept by hand
state_dir: /home/agent/agent-ops-state
capacity: 2

projects:
  # live since September
  - name: portfolio-eval
    paused: false   # claiming
    board: {owner: jesdi,
            number: 1}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 101
        # the gate runs before every PR
        clone_path: /home/agent/repos/portfolio_eval
        worktrees_path: /home/agent/repos/portfolio_eval.worktrees
        gate_cmd: "make gate ARGS='-k not slow' # all: of it"
        verify_cmd: 'make e2e SLOT={slot} MSG="a: b"'
        setup_cmd: >-
          uv sync &&
          pnpm install

  # paused while its board settles
  - name: web
    paused: true
    board:
      owner: jesdi
      number: 2
    repositories:
      - name: web
        repo: jesdi/web
        github_id: 303
        clone_path: /home/agent/repos/web/web
        worktrees_path: /home/agent/repos/web/web.worktrees
        gate_cmd: |
          pnpm lint
          pnpm test
"""
HAND_TAIL = """\

# limits
max_open: 10   # at least capacity"""


def test_a_hand_kept_targets_yaml_keeps_every_byte():
    from dispatcher.config import parse_config
    proposal = {"name": "analytics", "paused": True, "board": {"owner": "jesdi", "number": 4},
                "repositories": [{
                    "name": "analytics", "repo": "jesdi/analytics", "github_id": 202,
                    "clone_path": "/home/agent/repos/analytics/analytics",
                    "worktrees_path": "/home/agent/repos/analytics/analytics.worktrees",
                    "setup_cmd": "", "gate_cmd": "make gate # all: of it",
                    "verify_cmd": ""}]}
    block = yaml.safe_dump([proposal], sort_keys=False)
    indented = "".join(f"  {line}" for line in block.splitlines(keepends=True))
    out = add_project_text(HAND_HEAD + HAND_TAIL, block)
    assert out == HAND_HEAD + indented + HAND_TAIL
    cfg = parse_config(yaml.safe_load(out))
    assert [p.name for p in cfg.projects] == ["portfolio-eval", "web", "analytics"]
    assert cfg.repositories[-1].gate_cmd == "make gate # all: of it"
    assert cfg.max_open == 10
