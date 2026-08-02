"""Load targets.yaml into typed config objects."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from dispatcher.models import DEFAULT_POLICY, ModelPolicy, parse_policy


@dataclass(frozen=True)
class Target:
    name: str
    repo: str  # "owner/name"
    clone_path: str
    worktrees_path: str
    rank_cmd: str
    setup_cmd: str
    verify_cmd: str  # "{slot}" placeholder filled at spawn time
    project_number: int
    project_owner: str
    status_field_id: str
    status_ready_option_id: str
    status_in_progress_option_id: str
    boost_field_id: str = ""
    status_done_option_id: str = ""  # "" = never write Done to the board
    models: ModelPolicy | None = None  # None = inherit the global policy


@dataclass(frozen=True)
class Config:
    state_dir: str
    capacity: int
    budget_threshold: float
    racing_minutes: int
    racing_threshold: float
    session_memory: str
    session_cpus: str
    targets: list[Target]
    infra_repo: str = ""  # repo for dispatcher-side failure issues; "" degrades to ping-only
    models: ModelPolicy = DEFAULT_POLICY
    console_url: str = ""  # web console base URL for Telegram deep links; "" = no link line
    stall_after_seconds: int = 600  # 0 disables stall detection entirely
    # Minutes a finished spec waits at the review gate before the task parks:
    # session ended, capacity AND slot freed, so the dispatcher can keep
    # speccing the rest of the Ready queue overnight. 0 parks on the next pass.
    spec_review_grace_minutes: int = 15
    # Days a merged task's Done card stays on the console before its state
    # file is flushed. The durable record (merged PR, closed issue, board
    # item, event log) outlives the card.
    done_retention_days: int = 7
    triage_model: str = ""  # "" = use models.default for triage sessions
    # Minutes between dispatcher passes. Paired with OnUnitActiveSec in
    # provision/agent-ops-dispatcher.timer — change both together; the web
    # console's next-pass countdown is computed from this value.
    pass_interval_minutes: int = 10


def _target(raw: dict) -> Target:
    fields = dict(raw)
    has_models = "models" in fields
    models = fields.pop("models", None)
    # The key's PRESENCE decides override vs. inherit, not its truthiness —
    # `models: {}` must opt the target OUT of the global policy (empty rules,
    # plain default), not silently inherit it. Any other falsy value (`[]`,
    # `null`, `0`) means the same thing, since parse_policy maps them all to
    # DEFAULT_POLICY; a non-empty malformed value (`models: "x"`) still raises.
    return Target(**fields, models=parse_policy(models) if has_models else None)


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    return Config(
        state_dir=os.environ.get("AGENT_OPS_STATE_DIR", raw["state_dir"]),
        capacity=raw.get("capacity", 3),
        budget_threshold=raw.get("budget_threshold", 0.8),
        racing_minutes=raw.get("racing_minutes", 30),
        racing_threshold=raw.get("racing_threshold", 0.95),
        session_memory=str(raw.get("session_memory", "2g")),
        session_cpus=str(raw.get("session_cpus", "2")),
        targets=[_target(t) for t in raw.get("targets", [])],
        infra_repo=raw.get("infra_repo", ""),
        models=parse_policy(raw.get("models")),
        console_url=str(raw.get("console_url") or "").rstrip("/"),
        stall_after_seconds=int(raw.get("stall_after_seconds", 600)),
        spec_review_grace_minutes=int(raw.get("spec_review_grace_minutes", 15)),
        done_retention_days=int(raw.get("done_retention_days", 7)),
        triage_model=str(raw.get("triage_model", "")),
        pass_interval_minutes=int(raw.get("pass_interval_minutes", 10)),
    )


def policy_for(cfg: Config, target: Target) -> ModelPolicy:
    """A target's own policy replaces the global one wholesale — rule lists are
    never merged, because merge order would make first-match-wins ambiguous."""
    return target.models or cfg.models
