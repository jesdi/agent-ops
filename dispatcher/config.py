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
    )


def policy_for(cfg: Config, target: Target) -> ModelPolicy:
    """A target's own policy replaces the global one wholesale — rule lists are
    never merged, because merge order would make first-match-wins ambiguous."""
    return target.models or cfg.models
