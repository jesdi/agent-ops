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
    gate_cmd: "make gate"
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
    assert cfg.pace.budget_threshold == 0.8
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
    assert cfg.pace.budget_threshold == 0.8
    assert cfg.pace.racing_minutes == 30
    assert cfg.pace.racing_threshold == 0.95
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
    gate_cmd: "make gate"
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
  triage: [claude-sonnet-5@medium]
  untracked: standard
  tracks:
    standard:
      when: Bounded change with a clear scope.
      spec: [claude-opus-5@medium]
      plan: [claude-opus-5@medium]
      implement: [claude-sonnet-5@medium, openai/gpt-luna@xhigh]
      review: [openai/gpt-luna@xhigh, claude-sonnet-5@medium]
targets:
  - name: portfolio_eval
    repo: jesdi/portfolio_eval
    clone_path: /home/agent/repos/portfolio_eval
    worktrees_path: /home/agent/repos/portfolio_eval.worktrees
    rank_cmd: "rank"
    setup_cmd: "setup"
    verify_cmd: "make e2e-slot SLOT={slot}"
    gate_cmd: "make gate"
    project_number: 1
    project_owner: jesdi
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: I
    models:
      triage: [claude-fable-5-1]
      untracked: only
      tracks:
        only:
          when: Everything.
          spec: [claude-fable-5-1]
          plan: [claude-fable-5-1]
          implement: [claude-fable-5-1]
          review: [claude-fable-5-1]
"""


def test_global_policy_is_parsed(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    cfg = load_config(p)
    assert list(cfg.models.tracks) == ["standard"]
    assert cfg.models.untracked == "standard"
    assert [str(e) for e in cfg.models.tracks["standard"].stages["implement"]] == [
        "anthropic/claude-sonnet-5@medium", "openai/gpt-luna@xhigh"]
    assert str(cfg.models.triage[0]) == "anthropic/claude-sonnet-5@medium"


def test_target_policy_replaces_the_global_one(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    cfg = load_config(p)
    target = cfg.targets[0]
    assert list(policy_for(cfg, target).tracks) == ["only"]   # replaced wholesale, not merged


def test_target_without_models_inherits_the_global_policy(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)          # the module-level sample has no models: block
    cfg = load_config(p)
    assert policy_for(cfg, cfg.targets[0]) is cfg.models


def test_absent_models_block_is_the_default_policy(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    cfg = load_config(p)
    assert cfg.models == DEFAULT_POLICY


def test_old_models_shape_is_rejected_at_load(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\nmodels:\n  default: claude-opus-5\n  rules: []\n")
    with pytest.raises(ValueError, match="tracks"):
        load_config(p)


def test_top_level_triage_model_is_rejected_at_load(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\ntriage_model: claude-sonnet-5\n")
    with pytest.raises(ValueError, match="models.triage"):
        load_config(p)


def test_referenced_providers_come_from_every_entry(tmp_path: Path):
    from dispatcher.config import referenced_providers
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    assert referenced_providers(load_config(p)) == frozenset({"anthropic", "openai"})


WITH_EMPTY_TARGET_MODELS = """\
state_dir: /tmp/s
models:
  triage: [claude-opus-5]
  untracked: standard
  tracks:
    standard:
      when: Any task.
      spec: [claude-opus-5]
      plan: [claude-opus-5]
      implement: [claude-opus-5]
      review: [claude-opus-5]
targets:
  - name: portfolio_eval
    repo: jesdi/portfolio_eval
    clone_path: /home/agent/repos/portfolio_eval
    worktrees_path: /home/agent/repos/portfolio_eval.worktrees
    rank_cmd: "rank"
    setup_cmd: "setup"
    verify_cmd: "make e2e-slot SLOT={slot}"
    gate_cmd: "make gate"
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
    target out of global tracks (finding B)."""
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_EMPTY_TARGET_MODELS)
    cfg = load_config(p)
    target = cfg.targets[0]
    assert policy_for(cfg, target) == DEFAULT_POLICY
    # the global policy still has its own tracks — only the target opted out
    assert list(cfg.models.tracks) == ["standard"]


def test_stall_after_seconds_defaults_to_600(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    assert load_config(p).stall_after_seconds == 600


def test_stall_after_seconds_configurable_and_zero_disables(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nstall_after_seconds: 0\ntargets: []\n")
    assert load_config(p).stall_after_seconds == 0


def test_spec_review_grace_minutes_defaults_to_15(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    assert load_config(p).spec_review_grace_minutes == 15


def test_spec_review_grace_minutes_is_read_from_yaml(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nspec_review_grace_minutes: 0\ntargets: []\n")
    # 0 is a real value (park immediately), not "unset" — it must survive.
    assert load_config(p).spec_review_grace_minutes == 0


def write_yaml(tmp_path: Path, target_extra: dict | None = None, top_extra: dict | None = None) -> Path:
    """Helper to write a minimal targets.yaml with optional target and top-level extras.

    Returns the path to the written YAML file.
    """
    target_extra = target_extra or {}
    top_extra = top_extra or {}

    # Base target configuration
    target = {
        "name": "t",
        "repo": "o/r",
        "clone_path": "/c",
        "worktrees_path": "/w",
        "rank_cmd": "rank",
        "setup_cmd": "setup",
        "verify_cmd": "verify",
        "gate_cmd": "make gate",
        "project_number": 1,
        "project_owner": "o",
        "status_field_id": "F",
        "status_ready_option_id": "R",
        "status_in_progress_option_id": "I",
    }
    target.update(target_extra)

    # Build YAML content
    yaml_dict = {
        "state_dir": "/tmp/state",
        "targets": [target],
    }
    yaml_dict.update(top_extra)

    # Convert dict to YAML-like text (simple approach)
    import yaml
    p = tmp_path / "targets.yaml"
    p.write_text(yaml.dump(yaml_dict, default_flow_style=False))
    return p


def test_status_done_option_id_loaded_and_defaults_empty(tmp_path):
    cfg = load_config(write_yaml(tmp_path, target_extra={
        "status_done_option_id": "d0ne"}))
    assert cfg.targets[0].status_done_option_id == "d0ne"
    cfg = load_config(write_yaml(tmp_path))
    assert cfg.targets[0].status_done_option_id == ""


def test_status_wont_do_option_id_loaded_and_defaults_empty(tmp_path):
    cfg = load_config(write_yaml(tmp_path, target_extra={
        "status_wont_do_option_id": "w0nt"}))
    assert cfg.targets[0].status_wont_do_option_id == "w0nt"
    cfg = load_config(write_yaml(tmp_path))
    assert cfg.targets[0].status_wont_do_option_id == ""


def test_done_retention_days_loaded_and_defaults_seven(tmp_path):
    cfg = load_config(write_yaml(tmp_path, top_extra={"done_retention_days": 3}))
    assert cfg.done_retention_days == 3
    assert load_config(write_yaml(tmp_path)).done_retention_days == 7


def test_pass_interval_minutes_default_and_override(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    assert load_config(p).pass_interval_minutes == 10
    p.write_text("state_dir: /tmp/s\npass_interval_minutes: 5\ntargets: []\n")
    assert load_config(p).pass_interval_minutes == 5


GATED_YAML = """
state_dir: /tmp/s
targets:
  - name: alpha
    repo: jesdi/alpha
    clone_path: /tmp/c
    worktrees_path: /tmp/w
    rank_cmd: rank
    setup_cmd: setup
    verify_cmd: "make e2e-slot SLOT={slot}"
    gate_cmd: "make gate SLOT={slot}"
    project_number: 1
    project_owner: jesdi
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: P
"""


def test_gate_cmd_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"; p.write_text(GATED_YAML)
    assert load_config(p).targets[0].gate_cmd == "make gate SLOT={slot}"


def test_missing_gate_cmd_fails_at_load(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML.replace('    gate_cmd: "make gate SLOT={slot}"\n', ""))
    with pytest.raises(ValueError, match=r"alpha.*gate_cmd"):
        load_config(p)


def test_loop_caps_default_and_override(tmp_path, monkeypatch):
    from dispatcher.state import LoopCaps
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"; p.write_text(GATED_YAML)
    assert load_config(p).loop_caps == LoopCaps()
    p.write_text(GATED_YAML + "loop_caps:\n  gate: 1\n  ci: 5\n")
    assert load_config(p).loop_caps == LoopCaps(review=2, gate=1, e2e=3, ci=5)


def test_unknown_loop_cap_fails_at_load(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"; p.write_text(GATED_YAML + "loop_caps:\n  plan: 1\n")
    with pytest.raises(ValueError, match="loop_caps"):
        load_config(p)


from dispatcher.config import referenced_providers
from dispatcher.usage import PaceConfig


def test_pace_knobs_default(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)
    pc = load_config(p).pace
    assert (pc.budget_threshold, pc.racing_minutes, pc.racing_threshold) == (0.8, 30, 0.95)
    assert (pc.pace_margin, pc.weekend_weight, pc.timezone) == (0.10, 0.5, "UTC")
    assert pc == PaceConfig()


def test_pace_knobs_parse(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE.replace("budget_threshold: 0.8", "budget_threshold: 0.7")
                 + "pace_margin: 0.05\nweekend_weight: 0.25\ntimezone: Europe/Madrid\n")
    pc = load_config(p).pace
    assert (pc.pace_margin, pc.weekend_weight, pc.timezone) == (0.05, 0.25, "Europe/Madrid")
    assert pc.budget_threshold == 0.7


@pytest.mark.parametrize("extra, msg", [
    ("timezone: Mars/Olympus\n", "timezone"),
    ("weekend_weight: 1.5\n", "weekend_weight"),
    ("weekend_weight: -0.1\n", "weekend_weight"),
    ("pace_margin: -0.05\n", "pace_margin"),
    ("pace_margin: 1.0\n", "pace_margin"),
])
def test_bad_pace_knobs_fail_config_load(tmp_path, extra, msg):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + extra)
    with pytest.raises(ValueError, match=msg):
        load_config(p)


# --- ticket 01: config accepts the multi-project shape ---

TARGET_NO_OPTIONAL_CMDS = """
state_dir: /tmp/s
targets:
  - name: alpha
    repo: jesdi/alpha
    clone_path: /tmp/c
    worktrees_path: /tmp/w
    rank_cmd: rank
    gate_cmd: "make gate"
    project_number: 1
    project_owner: jesdi
    status_field_id: F
    status_ready_option_id: R
    status_in_progress_option_id: P
"""


def test_target_with_no_setup_or_verify_cmd_loads_with_empty_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(TARGET_NO_OPTIONAL_CMDS)
    t = load_config(p).targets[0]
    assert t.setup_cmd == ""
    assert t.verify_cmd == ""


def test_max_active_within_capacity_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + "    max_active: 2\n")
    assert load_config(p).targets[0].max_active == 2


@pytest.mark.parametrize("bad_value", [0, 4])  # capacity defaults to 3
def test_max_active_out_of_bounds_fails_naming_target_and_field(tmp_path, monkeypatch, bad_value):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + f"    max_active: {bad_value}\n")
    with pytest.raises(ValueError, match=r"alpha.*max_active"):
        load_config(p)


def test_spec_review_grace_minutes_null_means_never(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nspec_review_grace_minutes: null\ntargets: []\n")
    assert load_config(p).spec_review_grace_minutes is None


def test_missing_gate_cmd_error_names_target_and_field(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML.replace('    gate_cmd: "make gate SLOT={slot}"\n', ""))
    with pytest.raises(ValueError, match=r"alpha.*gate_cmd"):
        load_config(p)


def test_example_yaml_loads_cleanly_and_documents_new_fields():
    path = Path(__file__).resolve().parent.parent / "targets.example.yaml"
    cfg = load_config(path)
    assert cfg.targets  # loads cleanly end to end

    text = path.read_text()
    lines = text.splitlines()

    def _documented(field: str, keyword: str) -> bool:
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{field}:"):
                window = "\n".join(lines[max(0, i - 3):i + 1]).lower()
                return keyword in window
        return False

    assert "max_active" in text
    assert _documented("spec_review_grace_minutes", "null")
    assert _documented("setup_cmd", "optional")
    assert _documented("verify_cmd", "optional")


@pytest.mark.parametrize("bad_value", ["2", True, False, 2.5])
def test_max_active_wrong_type_fails_naming_target_and_field(tmp_path, monkeypatch, bad_value):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + f"    max_active: {bad_value!r}\n")
    with pytest.raises(ValueError, match=r"alpha.*max_active"):
        load_config(p)


@pytest.mark.parametrize("bad_value", ["15", True, False, 1.5])
def test_spec_review_grace_minutes_wrong_type_fails(tmp_path, bad_value):
    p = tmp_path / "targets.yaml"
    p.write_text(f"state_dir: /tmp/s\nspec_review_grace_minutes: {bad_value!r}\ntargets: []\n")
    with pytest.raises(ValueError, match="spec_review_grace_minutes"):
        load_config(p)


def test_referenced_providers_includes_target_policies(tmp_path):
    target_models = """\
    models:
      triage: [anthropic/m]
      untracked: t
      tracks:
        t:
          when: w
          spec: [openai/m]
          plan: [openai/m]
          implement: [openai/m]
          review: [openai/m]
"""
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE.replace("    status_in_progress_option_id: def456\n",
                                "    status_in_progress_option_id: def456\n" + target_models))
    assert referenced_providers(load_config(p)) == frozenset({"anthropic", "openai"})


CLAUDE_TRACKS_WITH_SECOND = """\
models:
  triage: [anthropic/m]
  untracked: t
  review_second: openai/gpt-5-codex
  tracks:
    t:
      when: w
      spec: [anthropic/m]
      plan: [anthropic/m]
      implement: [anthropic/m]
      review: [anthropic/m]
"""


@pytest.mark.parametrize("where", ["global", "target"])
def test_referenced_providers_includes_review_second(tmp_path, where):
    """Claude-only tracks plus review_second: openai must still be fetched,
    or the gate fails closed and the second model never fires."""
    p = tmp_path / "targets.yaml"
    if where == "global":
        text = CLAUDE_TRACKS_WITH_SECOND + SAMPLE
    else:
        text = SAMPLE.replace(
            "    status_in_progress_option_id: def456\n",
            "    status_in_progress_option_id: def456\n"
            + "".join("    " + line + "\n"
                      for line in CLAUDE_TRACKS_WITH_SECOND.splitlines()))
    p.write_text(text)
    assert referenced_providers(load_config(p)) == frozenset({"anthropic", "openai"})
