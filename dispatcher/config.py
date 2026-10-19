"""Load targets.yaml into typed config objects."""
from __future__ import annotations

import math
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from dispatcher import model_settings, models
from dispatcher.claims import PLACES
from dispatcher.models import DEFAULT_POLICY, ModelPolicy, parse_block, parse_policy
from dispatcher.state import LoopCaps, TaskState
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
    sandbox: bool = False  # legacy targets drain through their original runtime
    compose_services: tuple[str, ...] = ()
    sandbox_memory: int = 2 << 30
    sandbox_cpus: float = 2


@dataclass(frozen=True)
class ProjectRepository:
    """One repository owned by a project (see `Project`). Same fields a
    legacy `Target` carries for a repo, minus the identity it gets from its
    owning project instead (name, board)."""
    repo: str  # "owner/name", or a URL alias of it (normalized for dedup)
    clone_path: str
    worktrees_path: str
    rank_cmd: str
    gate_cmd: str
    setup_cmd: str = ""
    verify_cmd: str = ""
    boost_field_id: str = ""
    status_done_option_id: str = ""
    status_wont_do_option_id: str = ""
    max_active: int | None = None
    models: ModelPolicy | None = None
    compose_services: tuple[str, ...] = ()
    sandbox_memory: int = 2 << 30
    sandbox_cpus: float = 2

    @property
    def sandbox(self) -> bool:
        return True


@dataclass(frozen=True)
class Project:
    """A project owning one board and a nonempty set of repositories
    (`projects:`, parallel to the legacy single-repository `targets:` list).
    Paused (`enabled=False`) by default: onboarding a project must not start
    work until an operator turns it on."""
    key: str
    name: str
    board_owner: str
    board_number: int
    status_field_id: str
    status_ready_option_id: str
    status_in_progress_option_id: str
    repositories: list[ProjectRepository]
    enabled: bool = False


@dataclass(frozen=True)
class Config:
    state_dir: str
    capacity: int
    session_memory: str
    session_cpus: str
    targets: list[Target]
    projects: list[Project] = field(default_factory=list)
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


def _sandbox_config(fields):
    services = fields.get("compose_services", ())
    if not isinstance(services, (list, tuple)):
        raise ValueError("compose_services must be a list of service names")
    if any(not isinstance(name, str) or not name for name in services):
        raise ValueError("compose_services must contain nonempty service names")
    fields["compose_services"] = tuple(dict.fromkeys(services))
    for key in ("sandbox_memory", "sandbox_cpus"):
        _check_sandbox_quantity(key, fields.get(key, 1))


def _check_sandbox_quantity(key, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be a positive finite number")


def _target(raw: dict, capacity: int) -> Target:
    fields = dict(raw)
    _sandbox_config(fields)
    has_models = "models" in fields
    models = fields.pop("models", None)
    name = fields.get("name")
    if not str(fields.get("gate_cmd") or "").strip():
        raise ValueError(f"target {name!r}: gate_cmd is required "
                         "(the session runs it after every ticket)")
    max_active = fields.get("max_active")
    if max_active is not None and (
        isinstance(max_active, bool) or not isinstance(max_active, int)
        or not 1 <= max_active <= capacity
    ):
        raise ValueError(f"target {name!r}: max_active must be an integer between "
                         f"1 and capacity ({capacity}), got {max_active!r}")
    return Target(**fields, models=parse_policy(models) if has_models else None)


def normalize_repo(repo: str) -> str:
    """"owner/name", stripped of a URL alias's host/scheme/suffix, for
    identity comparison across projects. Lowered before the host regexes run
    so an upper-cased host (GITHUB.COM) still strips."""
    s = str(repo).strip().lower()
    s = re.sub(r"^ssh://git@github\.com/", "", s)
    s = re.sub(r"^git@github\.com:", "", s)
    s = re.sub(r"^https?://github\.com/", "", s)
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-len(".git")]
    return s.strip("/")


_REPOSITORY_STR_FIELDS = ("repo", "clone_path", "worktrees_path", "rank_cmd", "gate_cmd",
                          "setup_cmd", "verify_cmd", "boost_field_id",
                          "status_done_option_id", "status_wont_do_option_id")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_repository_fields(fields: dict, where: str) -> None:
    for name in _REPOSITORY_STR_FIELDS:
        if name in fields and not isinstance(fields[name], str):
            raise ValueError(f"{where}: {name} must be a string, got {fields[name]!r}")
    if not str(fields.get("gate_cmd") or "").strip():
        raise ValueError(f"{where}: gate_cmd is required "
                         "(the session runs it after every ticket)")
    max_active = fields.get("max_active")
    if max_active is not None and not _is_int(max_active):
        raise ValueError(f"{where}: max_active must be an integer, got {max_active!r}")


def _project_repository(raw: dict, project_key: str) -> ProjectRepository:
    fields = dict(raw)
    _sandbox_config(fields)
    has_models = "models" in fields
    models = fields.pop("models", None)
    where = f"project {project_key!r}: repository {fields.get('repo')!r}"
    _check_repository_fields(fields, where)
    try:
        return ProjectRepository(**fields, models=parse_policy(models) if has_models else None)
    except TypeError as e:
        raise ValueError(f"{where}: {e}") from e


def _project_repositories(repos_raw: object, key: object) -> list[ProjectRepository]:
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ValueError(f"project {key!r}: repositories must be a nonempty list")
    for r in repos_raw:
        if not isinstance(r, dict):
            raise ValueError(f"project {key!r}: repositories: each entry must be a "
                              f"mapping, got {r!r}")
    return [_project_repository(r, key) for r in repos_raw]


_PROJECT_STR_FIELDS = ("key", "name", "board_owner", "status_field_id",
                       "status_ready_option_id", "status_in_progress_option_id")


def _project(raw: dict, capacity: int) -> Project:
    fields = dict(raw)
    key = fields.get("key")
    for name in _PROJECT_STR_FIELDS:
        if name in fields and not isinstance(fields[name], str):
            raise ValueError(f"project {key!r}: {name} must be a string, got {fields[name]!r}")
    repositories = _project_repositories(fields.pop("repositories", None), key)
    enabled = fields.pop("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"project {key!r}: enabled must be a boolean, got {enabled!r}")
    board_number = fields.get("board_number")
    if not _is_int(board_number):
        raise ValueError(f"project {key!r}: board_number must be an integer, got {board_number!r}")
    try:
        return Project(**fields, repositories=repositories, enabled=enabled)
    except TypeError as e:
        raise ValueError(f"project {key!r}: {e}") from e


def _validate_identities(targets: list[Target], projects: list[Project]) -> None:
    """Repository identity (including URL aliases) and board identity
    (owner + number, case-insensitive) must each be unique across every
    project AND every legacy target (targets.example.yaml's migration
    contract). Project keys must also be unique and nonempty."""
    seen_repos: dict[str, str] = {}
    seen_boards: dict[tuple[str, int], str] = {}
    for t in targets:
        seen_repos[normalize_repo(t.repo)] = f"target {t.name!r}"
        seen_boards[(t.project_owner.strip().lower(), t.project_number)] = f"target {t.name!r}"

    seen_keys: set[str] = set()
    for p in projects:
        if not p.key or p.key in seen_keys:
            raise ValueError(f"project {p.key!r}: key must be nonempty and unique")
        seen_keys.add(p.key)
        board_key = (p.board_owner.strip().lower(), p.board_number)
        if board_key in seen_boards:
            raise ValueError(f"project {p.key!r}: board {p.board_owner}/{p.board_number} "
                             f"already owned by {seen_boards[board_key]}")
        seen_boards[board_key] = f"project {p.key!r}"
        for r in p.repositories:
            norm = normalize_repo(r.repo)
            if norm in seen_repos:
                raise ValueError(f"project {p.key!r}: repository {r.repo!r} already owned "
                                 f"by {seen_repos[norm]}")
            seen_repos[norm] = f"project {p.key!r}"


def _projects(raw: dict, capacity: int, targets: list[Target]) -> list[Project]:
    projects_raw = raw.get("projects", [])
    if not isinstance(projects_raw, list):
        raise ValueError(f"projects: must be a list, got {projects_raw!r}")
    for p in projects_raw:
        if not isinstance(p, dict):
            raise ValueError(f"projects: each entry must be a mapping, got {p!r}")
    projects = [_project(p, capacity) for p in projects_raw]
    _validate_identities(targets, projects)
    return projects


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
    targets = [_target(t, capacity) for t in raw.get("targets", [])]
    cfg = Config(
        state_dir=os.environ.get("AGENT_OPS_STATE_DIR", raw["state_dir"]),
        capacity=capacity,
        max_open=_max_open(raw, capacity),
        session_memory=str(raw.get("session_memory", "2g")),
        session_cpus=str(raw.get("session_cpus", "2")),
        targets=targets,
        projects=_projects(raw, capacity, targets),
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
    enabled = sum(p.enabled for p in cfg.projects)
    limit = min(cfg.max_open, PLACES)
    if enabled > limit:
        raise ValueError(f"projects: {enabled} enabled, at most {limit} "
                         "may be enabled (one reserved place each); pause one")
    _check_shares(cfg)
    return cfg


def project_target(p: Project, repo: str = "") -> Target:
    """The claim-round view of a project: a Target named by the project key
    that carries its board and the repository `repo` names (the first when
    `repo` is empty or unknown)."""
    r = next((r for r in p.repositories
              if repo and normalize_repo(r.repo) == normalize_repo(repo)),
             p.repositories[0])
    return Target(
        name=p.key, repo=r.repo, clone_path=r.clone_path,
        worktrees_path=r.worktrees_path, rank_cmd=r.rank_cmd,
        project_number=p.board_number, project_owner=p.board_owner,
        status_field_id=p.status_field_id,
        status_ready_option_id=p.status_ready_option_id,
        status_in_progress_option_id=p.status_in_progress_option_id,
        setup_cmd=r.setup_cmd, verify_cmd=r.verify_cmd, gate_cmd=r.gate_cmd,
        boost_field_id=r.boost_field_id,
        status_done_option_id=r.status_done_option_id,
        status_wont_do_option_id=r.status_wont_do_option_id,
        max_active=r.max_active, models=r.models, sandbox=True,
        compose_services=r.compose_services, sandbox_memory=r.sandbox_memory,
        sandbox_cpus=r.sandbox_cpus)


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


def policy_for(cfg: Config, target: Target | ProjectRepository | None) -> ModelPolicy:
    """The policy `target` runs: its own one replaces the global one
    wholesale — rule lists are never merged, because merge order would make
    first-match-wins ambiguous. No target (one removed from targets.yaml):
    the global one."""
    return (target.models if target else None) or cfg.models


def stranded(cfg: Config, target: Target | None, task: TaskState, stage: str) -> str:
    """models.stranded for `task` under `cfg`, launching `stage`."""
    policy = policy_for(cfg, target)
    # A task with no track is untracked work (state.launch_track).
    return models.stranded(policy, task.track or policy.untracked, task.picks,
                           stage, referenced_providers(cfg))


def _policies(cfg: Config) -> list[ModelPolicy]:
    """The global policy and every target's and project repository's own."""
    own = [*cfg.targets, *(r for p in cfg.projects for r in p.repositories)]
    return [cfg.models, *(t.models for t in own if t.models is not None)]


def routed_providers(cfg: Config) -> frozenset[str]:
    """Every provider some routed list names: the global triage list and the
    stage lists of every track, of the global policy and of every target's
    own. The priority mode may name one of these. Not routed: `review_second`,
    and a target policy's own triage list (the sweep runs `cfg.models.triage`)."""
    policies = _policies(cfg)
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
    policies = _policies(cfg)
    ids = [*(m for p in policies for m in p.model_ids()),
           *(p.review_second.model_id for p in policies if p.review_second)]
    return frozenset(m.partition("/")[0] for m in ids)
