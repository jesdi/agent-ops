"""Load targets.yaml into typed config objects."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from dispatcher import model_settings, models
from dispatcher.models import DEFAULT_POLICY, ModelPolicy, parse_block, parse_policy
from dispatcher.state import LoopCaps, TaskState
from dispatcher.usage import PaceConfig


@dataclass(frozen=True)
class Board:
    """A project's GitHub Projects board: where its claims and status writes
    go. Field and option ids are read from the live board by name
    (board.fields), never from config."""
    owner: str
    number: int


@dataclass(frozen=True)
class Project:
    name: str  # slug [a-z0-9-], unique on the box
    board: Board
    paused: bool = False  # True = no new claims; work already claimed carries on
    max_active: int | None = None  # None = uncapped; else 1 <= n <= capacity, validated at load
    models: ModelPolicy | None = None  # None = inherit the global policy


@dataclass(frozen=True)
class Repository:
    name: str
    repo: str  # "owner/name"
    github_id: int  # numeric GitHub repository id: what decides uniqueness
    clone_path: str
    worktrees_path: str
    project: Project
    setup_cmd: str = ""  # "" skips worktree provisioning in create_workspace
    verify_cmd: str = ""  # "{slot}" placeholder filled at spawn time; "" means no pre-PR e2e
    gate_cmd: str = ""  # repo-owned gate (tests/lint/CRAP); "{slot}" like verify_cmd. Required by load_config.


@dataclass(frozen=True)
class Config:
    state_dir: str
    capacity: int
    session_memory: str
    session_cpus: str
    repositories: list[Repository]
    infra_repo: str = ""  # repo for dispatcher-side failure issues; "" degrades to ping-only
    models: ModelPolicy = DEFAULT_POLICY
    console_url: str = ""  # web console base URL for Telegram deep links; "" = no link line
    stall_after_seconds: int = 600  # 0 disables stall detection entirely
    # Cap on a background wait: a session stopped on background work is
    # parked for the operator once the work has run this long.
    background_wait_seconds: int = 10800
    # Minutes a finished spec waits at the review gate before the task parks:
    # session ended, capacity AND slot freed, so the dispatcher can keep
    # speccing the rest of the Ready queue overnight. 0 parks on the next
    # pass; None (targets.yaml `null`) means never auto-park.
    spec_review_grace_minutes: int | None = 15
    # Box-wide cap on UNFINISHED tasks (running, parked, awaiting review or
    # CI, pr-open): the claim round stops at it even with capacity free.
    # Every unfinished task keeps a worktree on disk; this bounds that.
    max_open: int = 10
    # Days a finished task (done, failed, won't do) keeps its console card,
    # worktree and local branch before the flush removes them. The durable
    # record (PR, remote branch, issue, board item, event log) outlives it.
    done_retention_days: int = 7
    # Minutes between dispatcher passes. Paired with OnUnitActiveSec in
    # agent-ops-infra/provision/agent-ops-dispatcher.timer — change both together; the web
    # console's next-pass countdown is computed from this value.
    pass_interval_minutes: int = 5
    loop_caps: LoopCaps = LoopCaps()
    # The usage gate: session threshold and weekly pace
    # (see docs/specs/2026-09-13-usage-pace-gate-design.md).
    pace: PaceConfig = PaceConfig()
    # Where `models` comes from: load_config gives the targets.yaml block
    # (no origin); apply_stored_models lays the console's stored one over it.
    models_origin: model_settings.Origin = model_settings.Origin()

    @property
    def projects(self) -> list[Project]:
        """Every project, in config order: the ones `repositories` belong to
        (load_config refuses a project with none)."""
        return list({r.project.name: r.project for r in self.repositories}.values())


def by_project(repositories: list[Repository]) -> dict[str, list[Repository]]:
    """Project name -> its repositories among `repositories`, both in config
    order."""
    out: dict[str, list[Repository]] = {}
    for r in repositories:
        out.setdefault(r.project.name, []).append(r)
    return out


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


def _session_week_share(raw: object) -> dict[str, float]:
    """provider -> share of the week one session spends; each above 0 and at
    most 1."""
    if not isinstance(raw, dict):
        raise ValueError("session_week_share: must map provider to share, "
                         f"got {raw!r}")
    for provider, share in raw.items():
        if (isinstance(share, bool) or not isinstance(share, (int, float))
                or not 0.0 < share <= 1.0):
            raise ValueError(f"session_week_share: {provider} must be above 0 "
                             f"and at most 1, got {share!r}")
    return {str(provider): share for provider, share in raw.items()}


def _pace(raw: dict) -> PaceConfig:
    """The usage-gate knobs, read from their top-level targets.yaml keys."""
    d = PaceConfig()
    pace = PaceConfig(
        budget_threshold=raw.get("budget_threshold", d.budget_threshold),
        racing_minutes=raw.get("racing_minutes", d.racing_minutes),
        racing_threshold=raw.get("racing_threshold", d.racing_threshold),
        pace_margin=float(raw.get("pace_margin", d.pace_margin)),
        weekend_weight=float(raw.get("weekend_weight", d.weekend_weight)),
        timezone=str(raw.get("timezone", d.timezone)),
        session_week_share=_session_week_share(raw.get("session_week_share", {})))
    try:
        ZoneInfo(pace.timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"timezone: unknown IANA zone {pace.timezone!r}") from e
    if not 0.0 <= pace.weekend_weight <= 1.0:
        raise ValueError(f"weekend_weight: must be within 0..1, got {pace.weekend_weight}")
    if not 0.0 <= pace.pace_margin < 1.0:
        raise ValueError(f"pace_margin: must be at least 0 and below 1, got {pace.pace_margin}")
    return pace


_SLUG = re.compile(r"[a-z0-9-]+")
# Box-owned paths no clone or worktree may equal, contain or sit inside: the
# deployment checkout and the infra repo (state_dir is added per config).
_BOX_PATHS = ("~/agent-ops", "~/agent-ops-infra")


def _build(cls, owner: str, fields: dict):
    """`cls(**fields)`, a missing or unknown key named after `owner`."""
    try:
        return cls(**fields)
    except TypeError as e:
        raise ValueError(f"{owner}: {e}") from e


def _repositories(raw: dict, capacity: int, state_dir: str) -> list[Repository]:
    """Every repository of every project, in config order."""
    if "targets" in raw:
        raise ValueError("targets: is retired; rewrite it as projects: "
                         "(see targets.example.yaml)")
    repositories = [r for p in raw.get("projects") or [] for r in _project(p, capacity)]
    _check_unique(repositories)
    for r in repositories:
        _check_paths(r, state_dir)
    return repositories


def _project(raw: dict, capacity: int) -> list[Repository]:
    fields = dict(raw)
    name = fields.get("name")
    if not isinstance(name, str) or not _SLUG.fullmatch(name):
        raise ValueError(f"project {name!r}: name must be a slug of a-z, 0-9 and -")
    owner = f"project {name!r}"
    if "rank_cmd" in fields:
        del fields["rank_cmd"]
        print(f"[warn] {owner}: rank_cmd is ignored (the dispatcher ranks the "
              "board itself); remove it", file=sys.stderr)
    repositories = fields.pop("repositories", None)
    if not repositories:
        raise ValueError(f"{owner}: repositories must list at least one repository")
    if not isinstance(fields.setdefault("paused", False), bool):
        raise ValueError(f"{owner}: paused must be true or false, got {fields['paused']!r}")
    _check_max_active(owner, fields.get("max_active"), capacity)
    if "models" in fields:
        fields["models"] = parse_policy(fields["models"])
    fields["board"] = _board(owner, dict(fields.get("board") or {}))
    project = _build(Project, owner, fields)
    return [_repository(r, project) for r in repositories]


# Ids the board block held before they were read from the live board (#242).
_RETIRED_BOARD_KEYS = frozenset({
    "status_field_id", "status_ready_option_id", "status_in_progress_option_id",
    "status_done_option_id", "status_wont_do_option_id", "boost_field_id"})


def _board(owner: str, raw: dict) -> Board:
    """The board block; ids left over from the old shape are dropped with one
    warning, so a config written for the old code still loads."""
    retired = sorted(_RETIRED_BOARD_KEYS & set(raw))
    if retired:
        print(f"[warn] {owner}: board {', '.join(retired)} ignored (ids are "
              "read from the live board); remove them", file=sys.stderr)
    return _build(Board, f"{owner} board",
                  {k: v for k, v in raw.items() if k not in _RETIRED_BOARD_KEYS})


def _check_max_active(owner: str, max_active: object, capacity: int) -> None:
    if max_active is not None and (
        isinstance(max_active, bool) or not isinstance(max_active, int)
        or not 1 <= max_active <= capacity
    ):
        raise ValueError(f"{owner}: max_active must be an integer between "
                         f"1 and capacity ({capacity}), got {max_active!r}")


def _repository(raw: dict, project: Project) -> Repository:
    owner = f"repository {raw.get('name')!r} (project {project.name!r})"
    if not str(raw.get("gate_cmd") or "").strip():
        raise ValueError(f"{owner}: gate_cmd is required "
                         "(the session runs it after every ticket)")
    github_id = raw.get("github_id")
    if isinstance(github_id, bool) or not isinstance(github_id, int) or github_id < 1:
        raise ValueError(f"{owner}: github_id must be the numeric GitHub "
                         f"repository id, a positive integer, got {github_id!r}")
    return _build(Repository, owner, {**raw, "project": project})


def _owner(r: Repository) -> str:
    return f"repository {r.name!r} (project {r.project.name!r})"


def _check_unique(repositories: list[Repository]) -> None:
    """No project name, repository name, github_id or path twice on the box.
    clone_path and worktrees_path share one namespace: a clone can't double
    as another repository's worktrees either."""
    projects: dict[str, Project] = {}
    seen: dict[tuple, tuple[Repository, str]] = {}
    for r in repositories:
        if projects.setdefault(r.project.name, r.project) is not r.project:
            raise ValueError(f"project {r.project.name!r}: name used by two projects")
        for field in ("name", "github_id", "clone_path", "worktrees_path"):
            value = getattr(r, field)
            key = ("path", os.path.normpath(value)) if field.endswith("_path") else (field, value)
            other, other_field = seen.setdefault(key, (r, field))
            if other is not r or other_field != field:
                raise ValueError(f"{_owner(r)} {field} {value!r}: already the "
                                 f"{other_field} of {_owner(other)}")


def _check_paths(r: Repository, state_dir: str) -> None:
    """clone_path and worktrees_path are absolute and apart from the box's
    own checkouts and state: never equal to one, inside one or holding one."""
    roots = [os.path.expanduser(p) for p in _BOX_PATHS] + [state_dir]
    for field in ("clone_path", "worktrees_path"):
        path = getattr(r, field)
        if not os.path.isabs(path):
            raise ValueError(f"{_owner(r)}: {field} must be absolute, got {path!r}")
        for root in roots:
            a, b = os.path.normpath(path), os.path.abspath(root)
            if os.path.commonpath([a, b]) in (a, b):
                raise ValueError(f"{_owner(r)}: {field} {path!r} overlaps {root!r}: "
                                 "clones and worktrees live apart from the box's "
                                 "checkouts and state")


def _grace_minutes(raw: dict) -> int | None:
    if "spec_review_grace_minutes" not in raw:
        return 15
    value = raw["spec_review_grace_minutes"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"spec_review_grace_minutes: must be an integer or null, got {value!r}")
    if value < 0:
        raise ValueError(f"spec_review_grace_minutes: must be >= 0 or null, got {value}")
    return value


def _background_wait_seconds(raw: dict) -> int:
    v = raw.get("background_wait_seconds", 10800)
    if type(v) is not int or v < 1:  # bool is an int subclass: reject it too
        raise ValueError(f"background_wait_seconds: must be an integer >= 1, got {v!r}")
    return v


def _max_open(raw: dict, capacity: int) -> int:
    v = raw.get("max_open", 10)
    if type(v) is not int or v < capacity:
        raise ValueError(f"max_open: must be an integer >= capacity ({capacity}), got {v!r}")
    return v


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    if "triage_model" in raw:
        raise ValueError("triage_model: is gone; write models.triage: (a list of "
                         "entries, see targets.example.yaml)")
    if "codex_stall_after_seconds" in raw:
        print("[warn] codex_stall_after_seconds is ignored: stall_after_seconds "
              "now applies to every runtime", file=sys.stderr)
    capacity = raw.get("capacity", 3)
    state_dir = os.environ.get("AGENT_OPS_STATE_DIR", raw["state_dir"])
    cfg = Config(
        state_dir=state_dir,
        capacity=capacity,
        max_open=_max_open(raw, capacity),
        session_memory=str(raw.get("session_memory", "2g")),
        session_cpus=str(raw.get("session_cpus", "2")),
        repositories=_repositories(raw, capacity, state_dir),
        infra_repo=raw.get("infra_repo", ""),
        models=parse_policy(raw.get("models")),
        console_url=str(raw.get("console_url") or "").rstrip("/"),
        stall_after_seconds=int(raw.get("stall_after_seconds", 600)),
        background_wait_seconds=_background_wait_seconds(raw),
        spec_review_grace_minutes=_grace_minutes(raw),
        done_retention_days=int(raw.get("done_retention_days", 7)),
        pass_interval_minutes=int(raw.get("pass_interval_minutes", 5)),
        loop_caps=_loop_caps(raw.get("loop_caps")),
        pace=_pace(raw),
    )
    _check_shares(cfg)
    return cfg


def _check_shares(cfg: Config) -> None:
    # A share for a provider nothing runs on is a typo that would silently
    # turn the session-bound rule off.
    unknown = sorted(set(cfg.pace.session_week_share) - referenced_providers(cfg))
    if unknown:
        raise ValueError(f"session_week_share: {unknown} named by no model entry; "
                         f"expected any of {sorted(referenced_providers(cfg))}")


def with_models(cfg: Config, block: object) -> Config:
    """`cfg` with `block` (a raw `models:` mapping) as its global block,
    validated as load_config validates the targets.yaml one. Raises
    ValueError."""
    out = replace(cfg, models=parse_block(block))
    _check_shares(out)
    return out


def apply_stored_models(boot: Config) -> Config:
    """`boot` (load_config's targets.yaml config) with the block stored now
    in the state dir in force. Never raises: a bad stored block must not stop
    a pass, nor the console page that can fix it, so it is rejected and
    targets.yaml's block runs."""
    revision, found = model_settings.read(boot.state_dir)
    if not isinstance(found, model_settings.Found):
        return replace(boot, models_origin=model_settings.Origin(revision, found))
    try:
        cfg = with_models(boot, found.block)
    except ValueError as e:
        return replace(boot, models_origin=model_settings.Origin(
            revision, model_settings.Rejected(str(e))))
    stored = model_settings.Stored(found.set_by, found.set_at)
    return replace(cfg, models_origin=model_settings.Origin(revision, stored))


def policy_for(cfg: Config, repository: Repository | None) -> ModelPolicy:
    """The policy `repository` runs: its project's own one replaces the global one
    wholesale — rule lists are never merged, because merge order would make
    first-match-wins ambiguous. No repository (one removed from targets.yaml):
    the global one."""
    return (repository.project.models if repository else None) or cfg.models


def stranded(cfg: Config, repository: Repository | None, task: TaskState, stage: str) -> str:
    """models.stranded for `task` under `cfg`, launching `stage`."""
    return models.stranded(policy_for(cfg, repository), task.track, task.picks,
                           stage, referenced_providers(cfg))


def routed_providers(cfg: Config) -> frozenset[str]:
    """Every provider some routed list names: the global triage list and the
    stage lists of every track, of the global policy and of every project's
    own. The priority mode may name one of these. Not routed: `review_second`,
    and a project policy's own triage list (the sweep runs `cfg.models.triage`)."""
    policies = [cfg.models, *(p.models for p in cfg.projects if p.models is not None)]
    staged = (e for p in policies for t in p.tracks.values()
              for entries in t.stages.values() for e in entries)
    return frozenset(e.provider for e in (*cfg.models.triage, *staged))


def referenced_providers(cfg: Config) -> frozenset[str]:
    """Every provider some configured entry names — the set the dispatcher's
    usage fetch covers. A provider you hold credentials for but never route
    to is not polled for admission (the console still shows it while its
    adapter reads); one you route to without an adapter shows as
    unavailable (and main() warns once at startup). A policy's review_second counts too: the
    gate that grants it needs its provider's usage."""
    policies = [cfg.models, *(p.models for p in cfg.projects if p.models is not None)]
    ids = [*(m for p in policies for m in p.model_ids()),
           *(p.review_second.model_id for p in policies if p.review_second)]
    return frozenset(m.partition("/")[0] for m in ids)
