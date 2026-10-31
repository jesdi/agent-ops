import os
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
projects:
  - name: portfolio-eval
    board:
      owner: jesdi
      number: 1
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 101
        clone_path: /home/agent/repos/portfolio_eval
        worktrees_path: /home/agent/repos/portfolio_eval.worktrees
        setup_cmd: "scripts/setup-worktree.sh"
        verify_cmd: "make e2e-slot SLOT={slot}"
        gate_cmd: "make gate"
"""
# The project-level line a test appends a project key after (a models: block).
SAMPLE_PROJECT_LINE = '  - name: portfolio-eval\n'


def test_load_config(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)
    cfg = load_config(p)
    assert cfg.capacity == 3
    assert cfg.pace.budget_threshold == 0.8
    t = cfg.repositories[0]
    assert t.repo == "jesdi/portfolio_eval"
    assert t.verify_cmd == "make e2e-slot SLOT={slot}"
    assert t.github_id == 101
    assert t.project.name == "portfolio-eval"
    assert (t.project.board.owner, t.project.board.number) == ("jesdi", 1)
    assert t.project.paused is False
    assert cfg.projects == [t.project]


def test_defaults(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(
        "state_dir: /tmp/s\nprojects: []\n"
    )
    cfg = load_config(p)
    assert cfg.capacity == 3
    assert cfg.pace.budget_threshold == 0.8
    assert cfg.pace.racing_minutes == 30
    assert cfg.pace.racing_threshold == 0.95
    assert cfg.repositories == []


# --- env-var override tests (FIX #1) ---

def test_state_dir_env_overrides_yaml(tmp_path: Path, monkeypatch):
    """AGENT_OPS_STATE_DIR must win over whatever state_dir the YAML says."""
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /yaml/path\nprojects: []\n")
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", "/env/path")
    cfg = load_config(p)
    assert cfg.state_dir == "/env/path", (
        "state_dir should come from AGENT_OPS_STATE_DIR when the env var is set"
    )


def test_state_dir_falls_back_to_yaml_when_env_unset(tmp_path: Path, monkeypatch):
    """Without AGENT_OPS_STATE_DIR, state_dir must come from the YAML file."""
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /yaml/path\nprojects: []\n")
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    cfg = load_config(p)
    assert cfg.state_dir == "/yaml/path", (
        "state_dir should fall back to the YAML value when env var is absent"
    )


def test_session_caps_default(tmp_path):
    from dispatcher.config import load_config
    p = tmp_path / "t.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\n")
    cfg = load_config(p)
    assert cfg.session_memory == "2g" and cfg.session_cpus == "2"


def test_session_caps_overridable(tmp_path):
    from dispatcher.config import load_config
    p = tmp_path / "t.yaml"
    p.write_text("state_dir: /tmp/s\nsession_memory: 1500m\nsession_cpus: '1'\nprojects: []\n")
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
projects:
  - name: portfolio-eval
    board: {owner: jesdi, number: 1}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 101
        clone_path: /home/agent/repos/portfolio_eval
        worktrees_path: /home/agent/repos/portfolio_eval.worktrees
        setup_cmd: "setup"
        verify_cmd: "make e2e-slot SLOT={slot}"
        gate_cmd: "make gate"
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


def test_project_policy_replaces_the_global_one(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    cfg = load_config(p)
    repository = cfg.repositories[0]
    assert list(policy_for(cfg, repository).tracks) == ["only"]   # replaced wholesale, not merged


def test_project_without_models_inherits_the_global_policy(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE)          # the module-level sample has no models: block
    cfg = load_config(p)
    assert policy_for(cfg, cfg.repositories[0]) is cfg.models


def test_absent_models_block_is_the_default_policy(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\n")
    cfg = load_config(p)
    assert cfg.models == DEFAULT_POLICY


def test_old_models_shape_is_rejected_at_load(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\nmodels:\n  default: claude-opus-5\n  rules: []\n")
    with pytest.raises(ValueError, match="tracks"):
        load_config(p)


def test_top_level_triage_model_is_rejected_at_load(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\ntriage_model: claude-sonnet-5\n")
    with pytest.raises(ValueError, match="models.triage"):
        load_config(p)


def test_referenced_providers_come_from_every_entry(tmp_path: Path):
    from dispatcher.config import referenced_providers
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_MODELS)
    assert referenced_providers(load_config(p)) == frozenset({"anthropic", "openai"})


WITH_EMPTY_PROJECT_MODELS = """\
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
projects:
  - name: portfolio-eval
    board: {owner: jesdi, number: 1}
    repositories:
      - name: portfolio_eval
        repo: jesdi/portfolio_eval
        github_id: 101
        clone_path: /home/agent/repos/portfolio_eval
        worktrees_path: /home/agent/repos/portfolio_eval.worktrees
        setup_cmd: "setup"
        verify_cmd: "make e2e-slot SLOT={slot}"
        gate_cmd: "make gate"
    models: {}
"""


def test_project_with_empty_models_block_opts_out_of_global_policy(tmp_path: Path):
    """An explicit `models: {}` on a project means "override with nothing",
    not "inherit the global policy" — it's the natural way to opt one
    project out of global tracks (finding B)."""
    p = tmp_path / "targets.yaml"
    p.write_text(WITH_EMPTY_PROJECT_MODELS)
    cfg = load_config(p)
    repository = cfg.repositories[0]
    assert policy_for(cfg, repository) == DEFAULT_POLICY
    # the global policy still has its own tracks — only the project opted out
    assert list(cfg.models.tracks) == ["standard"]


def test_stall_after_seconds_defaults_to_600(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\n")
    cfg = load_config(p)
    assert cfg.stall_after_seconds == 600


def test_stall_after_seconds_configurable_and_zero_disables(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nstall_after_seconds: 0\nprojects: []\n")
    assert load_config(p).stall_after_seconds == 0


def test_spec_review_grace_minutes_defaults_to_15(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\n")
    assert load_config(p).spec_review_grace_minutes == 15


def test_spec_review_grace_minutes_is_read_from_yaml(tmp_path: Path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nspec_review_grace_minutes: 0\nprojects: []\n")
    # 0 is a real value (park immediately), not "unset" — it must survive.
    assert load_config(p).spec_review_grace_minutes == 0


def repository_dict(name="t", **extra) -> dict:
    """One repository entry of targets.yaml, its paths and github_id derived
    from `name` so two of them never collide."""
    return {"name": name, "repo": f"o/{name}", "github_id": sum(map(ord, name)),
            "clone_path": f"/c/{name}", "worktrees_path": f"/w/{name}",
            "setup_cmd": "setup", "verify_cmd": "verify", "gate_cmd": "make gate",
            **extra}


def project_dict(name="p", repositories=None, board_extra=None, **extra) -> dict:
    """One project entry of targets.yaml: one repository unless given."""
    board = {"owner": "o", "number": 1, **(board_extra or {})}
    return {"name": name, "board": board,
            "repositories": repositories or [repository_dict()], **extra}


def write_yaml(tmp_path: Path, repository_extra: dict | None = None,
               top_extra: dict | None = None, project_extra: dict | None = None,
               board_extra: dict | None = None, projects: list | None = None) -> Path:
    """A minimal targets.yaml: one project of one repository, each with its
    extras, or `projects` as given."""
    import yaml
    if projects is None:
        projects = [project_dict(repositories=[repository_dict(**(repository_extra or {}))],
                                 board_extra=board_extra, **(project_extra or {}))]
    p = tmp_path / "targets.yaml"
    p.write_text(yaml.dump({"state_dir": "/tmp/state", "projects": projects,
                            **(top_extra or {})}, default_flow_style=False))
    return p


def test_a_leftover_rank_cmd_is_ignored_with_a_warning(tmp_path, capsys):
    """The dispatcher ranks the board itself; a config written for the old
    code still loads, so the infra repo can drop the key after this ships."""
    cfg = load_config(write_yaml(tmp_path, project_extra={
        "rank_cmd": "python3 .claude/skills/backlog/rank.py --json"}))
    assert cfg.repositories[0].project.name == "p"
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "project 'p': rank_cmd is ignored" in err and "remove it" in err
    load_config(write_yaml(tmp_path))
    assert capsys.readouterr().err == ""


def test_leftover_board_ids_are_ignored_with_one_warning(tmp_path, capsys):
    """Board ids are read from the live board; a config written for the old
    code still loads, so the infra repo can drop the keys after this ships."""
    retired = {"status_field_id": "F", "status_ready_option_id": "R",
               "status_in_progress_option_id": "I",
               "status_done_option_id": "D", "status_wont_do_option_id": "W",
               "boost_field_id": "B"}
    cfg = load_config(write_yaml(tmp_path, board_extra=retired))
    board = cfg.repositories[0].project.board
    assert (board.owner, board.number) == ("o", 1)
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "project 'p': board " in err and "remove them" in err
    assert all(key in err for key in retired)
    load_config(write_yaml(tmp_path))
    assert capsys.readouterr().err == ""


def test_an_unknown_board_key_is_still_refused(tmp_path):
    with pytest.raises(ValueError, match="project 'p' board"):
        load_config(write_yaml(tmp_path, board_extra={"status_field": "F"}))


def test_done_retention_days_loaded_and_defaults_seven(tmp_path):
    cfg = load_config(write_yaml(tmp_path, top_extra={"done_retention_days": 3}))
    assert cfg.done_retention_days == 3
    assert load_config(write_yaml(tmp_path)).done_retention_days == 7


def test_pass_interval_minutes_default_and_override(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nprojects: []\n")
    assert load_config(p).pass_interval_minutes == 5
    p.write_text("state_dir: /tmp/s\npass_interval_minutes: 7\nprojects: []\n")
    assert load_config(p).pass_interval_minutes == 7


GATED_YAML = """
state_dir: /tmp/s
projects:
  - name: alpha
    board: {owner: jesdi, number: 1}
    repositories:
      - name: alpha
        repo: jesdi/alpha
        github_id: 1
        clone_path: /tmp/c
        worktrees_path: /tmp/w
        setup_cmd: setup
        verify_cmd: "make e2e-slot SLOT={slot}"
        gate_cmd: "make gate SLOT={slot}"
"""


def test_gate_cmd_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"; p.write_text(GATED_YAML)
    assert load_config(p).repositories[0].gate_cmd == "make gate SLOT={slot}"


def test_missing_gate_cmd_fails_at_load(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML.replace('        gate_cmd: "make gate SLOT={slot}"\n', ""))
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
    ("session_week_share: 0.05\n", "session_week_share"),
    ("session_week_share: {anthropic: true}\n", "session_week_share"),
    ("session_week_share: {anthropic: lots}\n", "session_week_share"),
])
def test_bad_pace_knobs_fail_config_load(tmp_path, extra, msg):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + extra)
    with pytest.raises(ValueError, match=msg):
        load_config(p)


def test_session_week_share_for_an_unreferenced_provider_fails_config_load(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + "session_week_share: {anthropc: 0.05}\n")
    with pytest.raises(ValueError, match="session_week_share.*anthropc"):
        load_config(p)
    p.write_text(SAMPLE + "session_week_share: {anthropic: 0.05}\n")
    assert dict(load_config(p).pace.session_week_share) == {"anthropic": 0.05}


# --- ticket 01: config accepts the multi-project shape ---

REPOSITORY_NO_OPTIONAL_CMDS = """
state_dir: /tmp/s
projects:
  - name: alpha
    board: {owner: jesdi, number: 1}
    repositories:
      - name: alpha
        repo: jesdi/alpha
        github_id: 1
        clone_path: /tmp/c
        worktrees_path: /tmp/w
        gate_cmd: "make gate"
"""


def test_repository_with_no_setup_or_verify_cmd_loads_with_empty_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(REPOSITORY_NO_OPTIONAL_CMDS)
    t = load_config(p).repositories[0]
    assert t.setup_cmd == ""
    assert t.verify_cmd == ""


def test_max_active_within_capacity_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + "    max_active: 2\n")
    assert load_config(p).repositories[0].project.max_active == 2


@pytest.mark.parametrize("bad_value", [0, 4])  # capacity defaults to 3
def test_max_active_out_of_bounds_fails_naming_project_and_field(tmp_path, monkeypatch, bad_value):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + f"    max_active: {bad_value}\n")
    with pytest.raises(ValueError, match=r"alpha.*max_active"):
        load_config(p)


def test_spec_review_grace_minutes_null_means_never(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\nspec_review_grace_minutes: null\nprojects: []\n")
    assert load_config(p).spec_review_grace_minutes is None


def test_missing_gate_cmd_error_names_repository_and_field(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML.replace('        gate_cmd: "make gate SLOT={slot}"\n', ""))
    with pytest.raises(ValueError, match=r"alpha.*gate_cmd"):
        load_config(p)


def test_example_yaml_loads_cleanly_and_documents_new_fields():
    path = Path(__file__).resolve().parent.parent / "targets.example.yaml"
    cfg = load_config(path)
    assert cfg.repositories  # loads cleanly end to end

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
def test_max_active_wrong_type_fails_naming_project_and_field(tmp_path, monkeypatch, bad_value):
    monkeypatch.delenv("AGENT_OPS_STATE_DIR", raising=False)
    p = tmp_path / "t.yaml"
    p.write_text(GATED_YAML + f"    max_active: {bad_value!r}\n")
    with pytest.raises(ValueError, match=r"alpha.*max_active"):
        load_config(p)


@pytest.mark.parametrize("bad_value", ["15", True, False, 1.5])
def test_spec_review_grace_minutes_wrong_type_fails(tmp_path, bad_value):
    p = tmp_path / "targets.yaml"
    p.write_text(f"state_dir: /tmp/s\nspec_review_grace_minutes: {bad_value!r}\nprojects: []\n")
    with pytest.raises(ValueError, match="spec_review_grace_minutes"):
        load_config(p)


def test_referenced_providers_includes_project_policies(tmp_path):
    repository_models = """\
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
    p.write_text(SAMPLE.replace(SAMPLE_PROJECT_LINE, SAMPLE_PROJECT_LINE + repository_models))
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


@pytest.mark.parametrize("where", ["global", "repository"])
def test_referenced_providers_includes_review_second(tmp_path, where):
    """Claude-only tracks plus review_second: openai must still be fetched,
    or the gate fails closed and the second model never fires."""
    p = tmp_path / "targets.yaml"
    if where == "global":
        text = CLAUDE_TRACKS_WITH_SECOND + SAMPLE
    else:
        text = SAMPLE.replace(
            SAMPLE_PROJECT_LINE, SAMPLE_PROJECT_LINE
            + "".join("    " + line + "\n"
                      for line in CLAUDE_TRACKS_WITH_SECOND.splitlines()))
    p.write_text(text)
    assert referenced_providers(load_config(p)) == frozenset({"anthropic", "openai"})


def test_routed_providers_are_the_lists_the_box_routes_not_review_second(tmp_path):
    """The set a priority mode may name: review_second is not a routed list,
    so naming its provider alone would order nothing."""
    from dispatcher.config import routed_providers
    p = tmp_path / "targets.yaml"
    p.write_text(CLAUDE_TRACKS_WITH_SECOND + SAMPLE)
    assert routed_providers(load_config(p)) == frozenset({"anthropic"})


TRIAGE, STAGE = (("triage: [anthropic/m]", "triage: [openai/m]"),
                 ("review: [anthropic/m]", "review: [anthropic/m, openai/m]"))


@pytest.mark.parametrize("where,swap,routed", [
    ("global", TRIAGE, {"anthropic", "openai"}),
    ("global", STAGE, {"anthropic", "openai"}),
    ("repository", STAGE, {"anthropic", "openai"}),
    # the sweep routes only the global triage list: a repository's is never run
    ("repository", TRIAGE, {"anthropic"}),
], ids=["global-triage", "global-stage", "repository-stage", "repository-triage"])
def test_routed_providers_are_the_global_triage_and_every_policys_stage_lists(
        tmp_path, where, swap, routed):
    from dispatcher.config import routed_providers
    models = CLAUDE_TRACKS_WITH_SECOND.replace(
        "  review_second: openai/gpt-5-codex\n", "").replace(*swap)
    assert "openai" in models
    if where == "global":
        text = models + SAMPLE
    else:
        text = SAMPLE.replace(
            SAMPLE_PROJECT_LINE, SAMPLE_PROJECT_LINE
            + "".join("    " + line + "\n" for line in models.splitlines()))
    p = tmp_path / "targets.yaml"
    p.write_text(text)
    assert routed_providers(load_config(p)) == frozenset(routed)


def test_max_open_loaded_defaults_ten_and_must_cover_capacity(tmp_path):
    assert load_config(write_yaml(tmp_path)).max_open == 10
    assert load_config(write_yaml(tmp_path, top_extra={"max_open": 12})).max_open == 12
    with pytest.raises(ValueError, match="max_open"):
        load_config(write_yaml(tmp_path, top_extra={"max_open": 1}))


# --- projects: the targets.yaml shape and its box-wide checks ---

def test_a_leftover_targets_key_is_refused(tmp_path):
    p = tmp_path / "targets.yaml"
    p.write_text("state_dir: /tmp/s\ntargets: []\n")
    with pytest.raises(ValueError, match=r"targets: is retired; rewrite it as projects: "
                                         r"\(see targets\.example\.yaml\)"):
        load_config(p)


def test_paused_is_parsed_and_must_be_a_boolean(tmp_path):
    assert load_config(write_yaml(tmp_path, project_extra={"paused": True})).projects[0].paused
    with pytest.raises(ValueError, match="project 'p': paused"):
        load_config(write_yaml(tmp_path, project_extra={"paused": "yes"}))


@pytest.mark.parametrize("name", ["Portfolio", "portfolio_eval", "a b", "", 7])
def test_a_project_name_must_be_a_slug(tmp_path, name):
    with pytest.raises(ValueError, match=r"project .*: name must be a slug"):
        load_config(write_yaml(tmp_path, projects=[project_dict(name=name)]))


def test_a_project_needs_a_repository(tmp_path):
    project = {**project_dict(), "repositories": []}
    with pytest.raises(ValueError, match="project 'p': repositories"):
        load_config(write_yaml(tmp_path, projects=[project]))


@pytest.mark.parametrize("github_id", [None, 0, -3, True, "123"])
def test_github_id_must_be_a_positive_integer(tmp_path, github_id):
    repository = repository_dict(github_id=github_id)
    if github_id is None:
        del repository["github_id"]
    with pytest.raises(ValueError, match=r"repository 't' \(project 'p'\): github_id"):
        load_config(write_yaml(tmp_path, projects=[project_dict(repositories=[repository])]))


def test_a_missing_board_key_names_the_project(tmp_path):
    project = project_dict()
    del project["board"]["owner"]
    with pytest.raises(ValueError, match="project 'p' board:.*owner"):
        load_config(write_yaml(tmp_path, projects=[project]))


@pytest.mark.parametrize("second, message", [
    # one project named twice
    (project_dict("p", [repository_dict("b")]), "project 'p': name used by two projects"),
    # the same key, each in its own project
    (project_dict("q", [repository_dict("a", repo="o/other")]),
     r"repository 'a' \(project 'q'\) name 'a': already the name of repository 'a' \(project 'p'\)"),
    (project_dict("q", [repository_dict("b", github_id=sum(map(ord, "a")))]),
     r"repository 'b' \(project 'q'\) github_id 97: already the github_id of repository 'a'"),
    (project_dict("q", [repository_dict("b", clone_path="/c/a/")]),
     r"repository 'b' \(project 'q'\) clone_path '/c/a/': already the clone_path of repository 'a'"),
    (project_dict("q", [repository_dict("b", worktrees_path="/w/a")]),
     r"repository 'b' \(project 'q'\) worktrees_path '/w/a': already the worktrees_path of repository 'a'"),
    # a clone can't double as another repository's worktrees
    (project_dict("q", [repository_dict("b", worktrees_path="/c/a")]),
     r"repository 'b' \(project 'q'\) worktrees_path '/c/a': already the clone_path of repository 'a'"),
], ids=["project-name", "repository-name", "github_id", "clone_path", "worktrees_path", "clone-as-worktrees"])
def test_duplicates_across_projects_are_refused_naming_both_owners(tmp_path, second, message):
    first = project_dict("p", [repository_dict("a")])
    with pytest.raises(ValueError, match=message):
        load_config(write_yaml(tmp_path, projects=[first, second]))


def test_repositories_flatten_in_config_order(tmp_path):
    cfg = load_config(write_yaml(tmp_path, projects=[
        project_dict("p", [repository_dict("a"), repository_dict("b")]),
        project_dict("q", [repository_dict("c")])]))
    assert [r.name for r in cfg.repositories] == ["a", "b", "c"]
    assert [p.name for p in cfg.projects] == ["p", "q"]
    assert cfg.repositories[0].project is cfg.repositories[1].project


@pytest.mark.parametrize("root", ["/home/agent/agent-ops", "/home/agent/agent-ops-infra",
                                  "/tmp/state"], ids=["checkout", "infra", "state_dir"])
@pytest.mark.parametrize("relation", ["equals", "inside", "contains"])
@pytest.mark.parametrize("field", ["clone_path", "worktrees_path"])
def test_paths_apart_from_the_box_checkouts_and_state(tmp_path, monkeypatch, root, relation, field):
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.delenv("AGENT_OPS_STATE_DIR")       # state_dir is /tmp/state
    path = {"equals": root + "/", "inside": root + "/x/y",
            "contains": os.path.dirname(root)}[relation]
    with pytest.raises(ValueError, match=rf"repository 't' \(project 'p'\): {field} .*overlaps"):
        load_config(write_yaml(tmp_path, repository_extra={field: path}))


def test_a_sibling_of_a_box_path_is_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/agent")
    cfg = load_config(write_yaml(tmp_path, repository_extra={
        "clone_path": "/home/agent/agent-ops-x", "worktrees_path": "/home/agent/agent-ops.wt"}))
    assert cfg.repositories[0].clone_path == "/home/agent/agent-ops-x"


@pytest.mark.parametrize("field", ["clone_path", "worktrees_path"])
def test_paths_must_be_absolute(tmp_path, field):
    with pytest.raises(ValueError, match=rf"repository 't' \(project 'p'\): {field} must be absolute"):
        load_config(write_yaml(tmp_path, repository_extra={field: "repos/t"}))
