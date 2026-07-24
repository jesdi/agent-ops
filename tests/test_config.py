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


# --- env-var override tests (FIX #1) ---

def test_state_dir_env_overrides_yaml(tmp_path: Path, monkeypatch):
    """AGENT_OPS_STATE_DIR must win over whatever state_dir the YAML says."""
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /yaml/path\ntargets: []\n")
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/env/path")
    cfg = load_config(p)
    assert cfg.state_dir == "/env/path", (
        "state_dir should come from AGENT_OPS_STATE_DIR when the env var is set"
    )


def test_state_dir_falls_back_to_yaml_when_env_unset(tmp_path: Path, monkeypatch):
    """Without AGENT_OPS_STATE_DIR, state_dir must come from the YAML file."""
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /yaml/path\ntargets: []\n")
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    cfg = load_config(p)
    assert cfg.state_dir == "/yaml/path", (
        "state_dir should fall back to the YAML value when env var is absent"
    )


def test_session_caps_default(tmp_path):
    from dispatcher.config import load_config
    p = tmp_path / "t.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    cfg = load_config(p)
    assert cfg.session_memory == "2g" and cfg.session_cpus == "2"


def test_session_caps_overridable(tmp_path):
    from dispatcher.config import load_config
    p = tmp_path / "t.yaml"
    p.write_text("state_dir: /tmp/s\nsession_memory: 1500m\nsession_cpus: '1'\ntargets: []\n")
    cfg = load_config(p)
    assert cfg.session_memory == "1500m" and cfg.session_cpus == "1"


_TARGET_YAML = """
state_dir: /tmp/state
targets:
  - name: t
    repo: o/r
    clone_path: /c
    worktrees_path: /w
    rank_cmd: rank
    setup_cmd: setup
    verify_cmd: verify
    project_number: 1
    project_owner: o
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: I
"""


def test_target_boost_field_id_parsed(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    path = tmp_path / "targets.yaml"
    path.write_text(_TARGET_YAML + "    boost_field_id: PVTF_B1\n")
    assert load_config(path).targets[0].boost_field_id == "PVTF_B1"


def test_target_boost_field_id_defaults_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    path = tmp_path / "targets.yaml"
    path.write_text(_TARGET_YAML)
    assert load_config(path).targets[0].boost_field_id == ""
