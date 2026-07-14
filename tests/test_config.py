from pathlib import Path

from dispatcher.config import load_config

SAMPLE = """\
capacity: 3
state_dir: /home/agent/agent-ops-state
budget_threshold: 0.8
racing_minutes: 30
racing_threshold: 0.95
targets:
  - name: portfolio_eval
    repo: jesdi/portfolio_eval
    clone_path: /home/agent/repos/portfolio_eval
    worktrees_path: /home/agent/repos/portfolio_eval.worktrees
    rank_cmd: "pipenv run python .claude/skills/backlog/rank.py --json"
    setup_cmd: "scripts/setup-worktree.sh"
    verify_cmd: "make e2e-slot SLOT={slot}"
    project_number: 1
    project_owner: jesdi
    status_field_id: PVTSSF_xxx
    status_ready_option_id: abc123
    status_in_progress_option_id: def456
"""


def test_load_config(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)
    cfg = load_config(p)
    assert cfg.capacity == 3
    assert cfg.budget_threshold == 0.8
    t = cfg.targets[0]
    assert t.repo == "jesdi/portfolio_eval"
    assert t.verify_cmd == "make e2e-slot SLOT={slot}"
    assert t.status_field_id == "PVTSSF_xxx"


def test_defaults(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        "state_dir: /tmp/s\ntargets: []\n"
    )
    cfg = load_config(p)
    assert cfg.capacity == 3
    assert cfg.budget_threshold == 0.8
    assert cfg.racing_minutes == 30
    assert cfg.racing_threshold == 0.95
    assert cfg.targets == []
