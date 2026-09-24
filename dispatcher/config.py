"""Load targets.yaml into typed config objects."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from dispatcher.models import DEFAULT_POLICY, ModelPolicy, parse_policy
from dispatcher.state import LoopCaps
from dispatcher.usage import PaceConfig


@dataclass(frozen=True)
class Target:
    name: str
    repo: str  # "owner/name"
    clone_path: str
    worktrees_path: str
    rank_cmd: str
    project_number: int
    project_owner: str
    status_field_id: str
    status_ready_option_id: str
    status_in_progress_option_id: str
    setup_cmd: str = ""  # "" skips worktree provisioning in create_workspace
    verify_cmd: str = ""  # "{slot}" placeholder filled at spawn time; "" means no pre-PR e2e
    gate_cmd: str = ""  # repo-owned gate (tests/lint/CRAP); "{slot}" like verify_cmd. Required by load_config.
    boost_field_id: str = ""
    status_done_option_id: str = ""  # "" = never write Done to the board
    status_wont_do_option_id: str = ""  # "" = cancel never touches the board
    max_active: int | None = None  # None = uncapped; else 1 <= n <= capacity, validated at load
    models: ModelPolicy | None = None  # None = inherit the global policy


@dataclass(frozen=True)
class Config:
    state_dir: str
    capacity: int
    session_memory: str
    session_cpus: str
    targets: list[Target]
    infra_repo: str = ""  # repo for dispatcher-side failure issues; "" degrades to ping-only
    models: ModelPolicy = DEFAULT_POLICY
    console_url: str = ""  # web console base URL for Telegram deep links; "" = no link line
    stall_after_seconds: int = 600  # 0 disables stall detection entirely
    # Minutes a finished spec waits at the review gate before the task parks:
    # session ended, capacity AND slot freed, so the dispatcher can keep
    # speccing the rest of the Ready queue overnight. 0 parks on the next
    # pass; None (targets.yaml `null`) means never auto-park.
    spec_review_grace_minutes: int | None = 15
    # Days a merged task's Done card stays on the console before its state
    # file is flushed. The durable record (merged PR, closed issue, board
    # item, event log) outlives the card.
    done_retention_days: int = 7
    # Minutes between dispatcher passes. Paired with OnUnitActiveSec in
    # agent-ops-infra/provision/agent-ops-dispatcher.timer — change both together; the web
    # console's next-pass countdown is computed from this value.
    pass_interval_minutes: int = 10
    loop_caps: LoopCaps = LoopCaps()
    # The usage gate: session threshold and weekly pace
    # (see docs/specs/2026-09-13-usage-pace-gate-design.md).
    pace: PaceConfig = PaceConfig()


def _loop_caps(raw: object) -> LoopCaps:
    if not raw:
        return LoopCaps()
    if not isinstance(raw, dict):
        raise ValueError(f"loop_caps: must be a mapping, got {raw!r}")
    unknown = set(raw) - set(LoopCaps.__dataclass_fields__)
    if unknown:
        raise ValueError(f"loop_caps: unknown key(s) {sorted(unknown)}; "
                         f"expected any of {sorted(LoopCaps.__dataclass_fields__)}")
    for k, v in raw.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"loop_caps: {k} must be a non-negative integer, got {v!r}")
    return LoopCaps(**raw)


def _pace(raw: dict) -> PaceConfig:
    """The usage-gate knobs, read from their top-level targets.yaml keys."""
    d = PaceConfig()
    pace = PaceConfig(
        budget_threshold=raw.get("budget_threshold", d.budget_threshold),
        racing_minutes=raw.get("racing_minutes", d.racing_minutes),
        racing_threshold=raw.get("racing_threshold", d.racing_threshold),
        pace_margin=float(raw.get("pace_margin", d.pace_margin)),
        weekend_weight=float(raw.get("weekend_weight", d.weekend_weight)),
        timezone=str(raw.get("timezone", d.timezone)))
    try:
        ZoneInfo(pace.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"timezone: unknown IANA zone {pace.timezone!r}") from e
    if not 0.0 <= pace.weekend_weight <= 1.0:
        raise ValueError(f"weekend_weight: must be within 0..1, got {pace.weekend_weight}")
    if not 0.0 <= pace.pace_margin < 1.0:
        raise ValueError(f"pace_margin: must be at least 0 and below 1, got {pace.pace_margin}")
    return pace


def _target(raw: dict, capacity: int) -> Target:
    fields = dict(raw)
    has_models = "models" in fields
    models = fields.pop("models", None)
    name = fields.get("name")
    if not str(fields.get("gate_cmd") or "").strip():
        raise ValueError(f"target {name!r}: gate_cmd is required "
                         "(the session runs it after every ticket)")
    max_active = fields.get("max_active")
    if max_active is not None and not 1 <= max_active <= capacity:
        raise ValueError(f"target {name!r}: max_active must be between 1 and "
                         f"capacity ({capacity}), got {max_active}")
    return Target(**fields, models=parse_policy(models) if has_models else None)


def _grace_minutes(raw: dict) -> int | None:
    if "spec_review_grace_minutes" not in raw:
        return 15
    value = raw["spec_review_grace_minutes"]
    if value is None:
        return None
    value = int(value)
    if value < 0:
        raise ValueError(f"spec_review_grace_minutes: must be >= 0 or null, got {value}")
    return value


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    if "triage_model" in raw:
        raise ValueError("triage_model: is gone; write models.triage: (a list of "
                         "entries, see targets.example.yaml)")
    return Config(
        state_dir=os.environ.get("AGENT_OPS_STATE_DIR", raw["state_dir"]),
        capacity=raw.get("capacity", 3),
        session_memory=str(raw.get("session_memory", "2g")),
        session_cpus=str(raw.get("session_cpus", "2")),
        targets=[_target(t, raw.get("capacity", 3)) for t in raw.get("targets", [])],
        infra_repo=raw.get("infra_repo", ""),
        models=parse_policy(raw.get("models")),
        console_url=str(raw.get("console_url") or "").rstrip("/"),
        stall_after_seconds=int(raw.get("stall_after_seconds", 600)),
        spec_review_grace_minutes=_grace_minutes(raw),
        done_retention_days=int(raw.get("done_retention_days", 7)),
        pass_interval_minutes=int(raw.get("pass_interval_minutes", 10)),
        loop_caps=_loop_caps(raw.get("loop_caps")),
        pace=_pace(raw),
    )


def policy_for(cfg: Config, target: Target) -> ModelPolicy:
    """A target's own policy replaces the global one wholesale — rule lists are
    never merged, because merge order would make first-match-wins ambiguous."""
    return target.models or cfg.models


def referenced_providers(cfg: Config) -> frozenset[str]:
    """Every provider some configured entry names — the set the usage fetch
    covers. A provider you hold credentials for but never route to is not
    polled; one you route to without an adapter shows as unavailable (and
    main() warns once at startup)."""
    ids = cfg.models.model_ids()
    for t in cfg.targets:
        if t.models is not None:
            ids.extend(t.models.model_ids())
    return frozenset(m.partition("/")[0] for m in ids)
