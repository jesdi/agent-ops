"""Load targets.yaml into typed config objects."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


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
        targets=[Target(**t) for t in raw.get("targets", [])],
    )
