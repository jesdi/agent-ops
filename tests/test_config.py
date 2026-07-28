from pathlib import Path

import pytest

from dispatcher.config import load_config, policy_for
from dispatcher.models import DEFAULT_POLICY

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


def test_infra_repo_defaults_to_empty(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)
    assert load_config(p).infra_repo == ""


def test_infra_repo_loaded(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("infra_repo: jesdi/agent-ops\n" + SAMPLE)
    assert load_config(p).infra_repo == "jesdi/agent-ops"


def test_console_url_defaults_to_empty(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)
    assert load_config(p).console_url == ""


def test_console_url_loaded_and_trailing_slash_stripped(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("console_url: https://box.tail.ts.net/\n" + SAMPLE)
    assert load_config(p).console_url == "https://box.tail.ts.net"


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


WITH_MODELS = """\
state_dir: /tmp/s
models:
  default: claude-opus-4-8
  rules:
    - name: trivial-backend
      when:
        effort: {max: 1}
        labels_exclude: [frontend]
      use: claude-sonnet-4-6
targets:
  - name: portfolio_eval
    repo: jesdi/portfolio_eval
    clone_path: /home/agent/repos/portfolio_eval
    worktrees_path: /home/agent/repos/portfolio_eval.worktrees
    rank_cmd: "rank"
    setup_cmd: "setup"
    verify_cmd: "make e2e-slot SLOT={slot}"
    project_number: 1
    project_owner: jesdi
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: I
    models:
      default: claude-fable-5
      rules: []
"""


def test_global_policy_is_parsed(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    cfg = load_config(p)
    assert cfg.models.default == "claude-opus-4-8"
    assert [r.name for r in cfg.models.rules] == ["trivial-backend"]
    assert cfg.models.rules[0].effort_max == 1
    assert cfg.models.rules[0].labels_exclude == ("frontend",)


def test_target_policy_replaces_the_global_one(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    cfg = load_config(p)
    target = cfg.targets[0]
    assert policy_for(cfg, target).default == "claude-fable-5"
    assert policy_for(cfg, target).rules == ()   # replaced wholesale, not merged


def test_target_without_models_inherits_the_global_policy(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)          # the module-level sample has no models: block
    cfg = load_config(p)
    assert policy_for(cfg, cfg.targets[0]) is cfg.models


def test_absent_models_block_defaults_to_opus(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    cfg = load_config(p)
    assert cfg.models == DEFAULT_POLICY
    assert cfg.models.default == "claude-opus-4-8"


WITH_EMPTY_TARGET_MODELS = """\
state_dir: /tmp/s
models:
  default: claude-opus-4-8
  rules:
    - name: trivial-backend
      when:
        effort: {max: 1}
        labels_exclude: [frontend]
      use: claude-sonnet-4-6
targets:
  - name: portfolio_eval
    repo: jesdi/portfolio_eval
    clone_path: /home/agent/repos/portfolio_eval
    worktrees_path: /home/agent/repos/portfolio_eval.worktrees
    rank_cmd: "rank"
    setup_cmd: "setup"
    verify_cmd: "make e2e-slot SLOT={slot}"
    project_number: 1
    project_owner: jesdi
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: I
    models: {}
"""


def test_target_with_empty_models_block_opts_out_of_global_policy(tmp_path: Path):
    """An explicit `models: {}` on a target means "override with nothing",
    not "inherit the global policy" — it's the natural way to opt one
    target out of global rules (finding B)."""
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_EMPTY_TARGET_MODELS)
    cfg = load_config(p)
    target = cfg.targets[0]
    assert policy_for(cfg, target) == DEFAULT_POLICY
    assert policy_for(cfg, target).rules == ()
    assert policy_for(cfg, target).default == "claude-opus-4-8"
    # the global policy still has its rule — only the target opted out
    assert cfg.models.rules != ()


def test_malformed_rule_fails_at_load(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        "state_dir: /tmp/s\ntargets: []\n"
        "models:\n"
        "  default: claude-opus-4-8\n"
        "  rules:\n"
        "    - name: typo\n"
        "      when: {label_include: [frontend]}\n"
        "      use: claude-sonnet-4-6\n"
    )
    with pytest.raises(ValueError, match="label_include"):
        load_config(p)


def test_stall_after_seconds_defaults_to_600(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    assert load_config(p).stall_after_seconds == 600


def test_stall_after_seconds_configurable_and_zero_disables(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nstall_after_seconds: 0\ntargets: []\n")
    assert load_config(p).stall_after_seconds == 0
