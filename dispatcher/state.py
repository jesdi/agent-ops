"""Per-task runtime state (~/agent-ops-state/task-<target>-<issue>.json) and the
stage.json signal sessions write into their worktree."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import warnings
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, NamedTuple

from dispatcher.models import (FEEDBACK_PICK, IMPLEMENT_PICK, Entry,
                               ModelPolicy, Order, candidates, pick_provider,
                               review_avoid)


class Stage(str, Enum):
    QUEUED = "queued"
    SPEC = "spec"
    AWAITING_PLAN_REVIEW = "awaiting-plan-review"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"           # fresh-eyes review; rebases, verifies, opens the PR
    PR_OPEN = "pr-open"
    ADDRESS_REVIEW = "address-review"
    BLOCKED = "blocked"
    FAILED = "failed"
    STALLED_ON_BUDGET = "stalled-on-budget"
    DONE = "done"
    CANCELED = "canceled"


TERMINAL_STAGES = frozenset({Stage.DONE, Stage.FAILED, Stage.CANCELED})


# Stages that occupy capacity and an E2E slot. BLOCKED and
# STALLED_ON_BUDGET still hold a live session/worktree, so they count.
IN_FLIGHT_STAGES = frozenset({
    Stage.QUEUED, Stage.SPEC, Stage.PLAN, Stage.AWAITING_PLAN_REVIEW,
    Stage.IMPLEMENT, Stage.REVIEW, Stage.ADDRESS_REVIEW, Stage.BLOCKED,
    Stage.STALLED_ON_BUDGET,
})

# Stages a session runs from its own stage prompt, so a crashed one can be
# retried by spawning the stage afresh from the worktree's artifacts.
RESPAWNABLE_STAGES = frozenset({
    Stage.SPEC, Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW, Stage.ADDRESS_REVIEW,
})


def resumable_crash(t: "TaskState") -> bool:
    return t.stage is Stage.FAILED and bool(t.crashed_stage)


class NextLaunch(NamedTuple):
    stage: str        # runtime stage


# A stage with no session of its own -> the stage its next launch runs: a
# parked pr-open task wakes into an address-review round, and a plan that
# waits at its review gate is continued, or respawned, as the plan stage.
_LAUNCHED_AS = {Stage.PR_OPEN.value: Stage.ADDRESS_REVIEW.value,
                Stage.AWAITING_PLAN_REVIEW.value: Stage.PLAN.value}


def next_launch(t: "TaskState") -> NextLaunch:
    """A task's next launch, read from the persisted state: the one answer
    the dispatcher, the status lines and the console share. A crashed task
    resumes the stage it crashed in. After the implement session is done
    the next launch is review, also while review waits for the gate."""
    stage = t.crashed_stage if resumable_crash(t) else t.stage.value
    if stage == Stage.IMPLEMENT.value and implement_done(t):
        return NextLaunch(Stage.REVIEW.value)
    return NextLaunch(_LAUNCHED_AS.get(stage, stage))


def implement_done(t: "TaskState") -> bool:
    """The implement session reported done and review has not started: a
    provider ran implement and its pick is gone (dropped, in a write of its
    own, when review is chosen). Before the first implement launch there is
    no provider; while the session runs, or waits parked, it has its pick."""
    return bool(t.implement_providers) and IMPLEMENT_PICK not in t.picks


def shown_stage(t: "TaskState") -> str:
    """The stage a status line and a card label a task with: its own, but a
    task in implement is labelled with its next launch, which is review once
    the implement session is done."""
    return next_stage(t) if t.stage is Stage.IMPLEMENT else t.stage.value


def next_stage(t: "TaskState") -> str:
    """The runtime stage a task's next launch runs (next_launch)."""
    return next_launch(t).stage


@dataclass(frozen=True)
class LoopCaps:
    """Rounds each bounded loop may run before the task parks. Defaults are
    the spec's; targets.yaml `loop_caps:` overrides any of them."""
    review: int = 2   # review-stage fix rounds
    gate: int = 2     # rounds of a session-reported gate loop
    e2e: int = 3      # failed end-to-end runs (implement/review)
    ci: int = 3       # fixes on an open PR (red check, conflict, failed run)

@dataclass(frozen=True)
class PlanApprovalRequest:
    path: str   # the plan session's review page, worktree-relative
    kind: str = "plan-approval"
    # The plan revision the operator is asked to approve (see
    # artifacts.plan_revision). "" = unknown: such a request matches no plan.
    # Not part of the request's identity, and not sent to the console.
    fingerprint: str = field(default="", compare=False)

    def __post_init__(self):
        if not self.path:
            raise ValueError("plan-approval request requires a non-empty path")


@dataclass(frozen=True)
class AnswersRequest:
    path: str
    kind: str = "answers"
    # SHA-256 of the page bytes when the request was armed: the revision an
    # answers intent must name. "" = unknown: such a request matches nothing.
    fingerprint: str = field(default="", compare=False)

    def __post_init__(self):
        if not self.path:
            raise ValueError("answers request requires a non-empty path")


OperatorRequest = PlanApprovalRequest | AnswersRequest  # type alias

# A task that holds no E2E slot. Every session-ending park releases its slot
# back to the pool; only PARK_LOGIN keeps a slot because it keeps a live
# container and session running (the pane is where the operator types the
# OAuth code). PARK_REVIEW is the original exception that freed the slot; the
# rule now applies to all session-ending parks, so slots are never leaked.
NO_SLOT = -1

# Park lifecycle (orthogonal to stage — the stage is preserved while parked).
# "" = not parked. Parked tasks release CAPACITY. Every park that ends the
# session also releases its SLOT — the only exception is PARK_LOGIN, which
# keeps a live container and session and therefore keeps both capacity
# and the slot (see active() and holds_slot()).
PARK_HUMAN = "parked"            # waiting for operator input
PARK_CI = "awaiting-ci"          # waiting for a GitHub Actions run
PARK_WAKE = "unpark-requested"   # wake event arrived; resume when slot free
PARK_LOGIN = "parked-login"      # live session sitting at a /login prompt
PARK_REVIEW = "awaiting-review"  # plan ready, parked for review at leisure

# Why a PARK_HUMAN task waits (TaskState.park_reason); the board shows it.
WHY_DECISION = "decision"        # the session asks the operator something
WHY_STALLED = "stalled"          # silent, poked out, or a wait past its cap
WHY_LOOP_CAP = "loop cap"        # a bounded loop used all its rounds
WHY_BAD_SIGNAL = "bad signal"    # stage.json still rejected after the retries
WHY_CONFIG = "config"            # the config strands the task (models.stranded)
WHY_OPERATOR = "operator"        # the operator parked it

# No poke stretch (TaskState.poked_since / pokes): `replace(task, **NO_POKES)`
# at the clear points named on those fields.
NO_POKES = MappingProxyType({"poked_since": 0.0, "pokes": 0})


@dataclass(frozen=True)
class RequiredPR:
    """One repository-qualified PR a task needs merged. `state` is open |
    merged | closed (closed without merge); the cursors and the CI-fix
    counter belong to this PR alone, so equal numbers in two repositories
    never share them."""
    repo: str
    number: int
    state: str = "open"
    head: str = ""
    check_cursor: str = ""
    feedback_cursor: str = ""
    conflict_cursor: str = ""
    ci_fix_attempts: int = 0
    feedback_rounds: int = 0
    outcome: str = ""      # the last poll's classification: quiet | feedback | check-failed | ...
    checks: str = ""       # the last CI read of this PR: check-failed | pending | passing | "" unread


CI_FIX_LIMIT = 3     # automated CI-fix attempts per required PR


@dataclass(frozen=True)
class TaskState:
    issue: int
    target: str
    stage: Stage
    slot: int
    worktree: str
    branch: str
    title: str
    updated_at: str  # read by main._grace_elapsed; touching this on a gate-parked task restarts its review clock
    park: str = ""
    ci_run_id: int = 0
    park_msg_id: int = 0
    park_note: str = ""                  # the question shown while parked
    park_reason: str = ""                # WHY_* of the last PARK_HUMAN; "" = from before the field
    hold_for_attach: bool = False
    effort: int | None = None            # board Effort at claim time
    labels: tuple[str, ...] = ()         # board labels at claim time
    track: str = ""                      # configured track name (spec/2026-09-14-model-tracks)
    # providers that ran implement: first use first, no repeats
    implement_providers: list[str] = field(default_factory=list)
    # models.pick_key(stage) -> "provider/model[@effort]", sticky per key;
    # the implement pick until review starts
    picks: dict[str, str] = field(default_factory=dict)
    spec_retries: int = 0                # in-session spec-signal retries used (bad/missing track, awaiting-review)
    plan_retries: int = 0                # in-session plan-format retries used
    plan_slips: int = 0                  # in-session plan-signal retries used (unapproved done, bad track)
    # Plan gate: respawns of a dead session plus review rounds started since
    # the operator last acted. Owned by dispatcher/loops.py (gate_fields
    # counts, reset on an operator wake, UNATTENDED_ROUND_LIMIT caps).
    unattended_rounds: int = 0
    # A gate respawn already spent the round for its first valid ready report.
    gate_respawn_pending: bool = False
    pr_number: int = 0                   # the task's PR; 0 = not yet resolved
    feedback_cursor: str = ""            # ISO ts; "" = any human feedback is new
    feedback_pending: bool = False       # feedback seen, address-review deferred
    terminal_at: str = ""                # first terminal transition; cleared on reopening
    done_at: str = ""                    # merge-detection time; drives the flush
    spec_path: str = ""                  # approved spec, worktree-relative or absolute
    ticket_count: int = 0                # size of .agent/tickets/ when implement started
    # The task entered the plan review gate. Set only through
    # loops.gate_fields, never cleared: a task that waited for the operator
    # once never skips the gate, whatever stage a respawn puts it back in.
    gated: bool = False
    # A spec- or plan-stage session parked for answers. Set only by
    # _park_for_input in dispatcher/main.py, never cleared: it outlives the
    # session that asked.
    asked: bool = False
    # Round counters, one per bounded loop. Owned by the dispatcher: a
    # session reports rounds but can never lower these.
    review_rounds: int = 0
    gate_rounds: int = 0
    e2e_rounds: int = 0
    ci_rounds: int = 0
    check_cursor: str = ""               # completedAt of the newest red check acted on
    conflict_cursor: str = ""            # head sha of the last conflict acted on
    attention: str = ""                  # why address-review is pending: feedback|check-failed|conflict|operator
    # One-shot operator choice for a wake already queued behind admission.
    # Cleared after the session successfully resumes.
    resume_model_override: str = ""
    resume_bypass_usage: bool = False
    # The stage a crash failed this task out of; Resume respawns it fresh in
    # the same worktree. "" = not resumable (kill, closed PR, pre-field crash).
    crashed_stage: str = ""
    # The background wait the dispatcher has seen (see machine.pass_actions):
    # the marker's `reported` it last recorded, and herdr's state-change
    # counter at that moment. A different counter later means a new turn.
    background_reported: float = 0.0
    background_seq: int = 0
    # Pokes of a stopped session (machine.Poke): when the first of this
    # stretch was attempted, and how many were delivered. A stretch belongs
    # to one live session: every session start clears it (NO_POKES), and so
    # does what shows that the live session moved on (herdr sees it working,
    # a background wait is recorded) and the login park, which keeps the
    # pane. A park or a crash that ends the session leaves the old numbers
    # on the task until the next start.
    poked_since: float = 0.0
    pokes: int = 0
    # None=no request; PlanApprovalRequest while at gate; AnswersRequest written
    # ONLY by _park_for_input in dispatcher/main.py, and only when a
    # worktree-contained path resolves — so an answers request never exists
    # without a valid path.
    operator_request: "OperatorRequest | None" = None
    # Canonical "owner/name" of the origin repository. "" = a legacy record
    # whose repository is not proven (see migrate_legacy).
    repo: str = ""
    # The PRs this task needs merged; non-empty = authoritative, the task is
    # done only when every one is merged. The revision names that set.
    required_prs: tuple[RequiredPR, ...] = ()
    required_set_revision: int = 0
    # "owner/name#N" of the PRs the queued fix round repairs; each is charged
    # when the round is actually launched, not when the failure is observed.
    repair_prs: tuple[str, ...] = ()
    # The PR a review-feedback round repairs; feedback is not a CI attempt.
    feedback_pr: str = ""
    # Automated CI fixes spent on the single PR; feedback and resume never reset it.
    ci_fix_attempts: int = 0
    # The partial-merge outcome the operator was last asked about.
    partial_incident: str = ""
    # Durable send reservation: an ambiguous transport outcome is never replayed.
    partial_delivery: str = ""
    # Charged before spawning; an interrupted launch requires reconciliation.
    repair_launch_reserved: bool = False
    ci_repair_pending: bool = False  # failed verification wake awaiting repair admission
    # Explicit operator exclusions survive discovery and bundle validation.
    dropped_pr_repos: tuple[str, ...] = ()

    @property
    def continued_stage(self) -> Stage:
        """The stage whose conversation a continuation belongs to."""
        return Stage.PLAN if self.stage is Stage.AWAITING_PLAN_REVIEW else self.stage


def launch_track(t: TaskState, policy: ModelPolicy) -> str:
    """The track whose list t's next launch (next_launch) reads, or "" when
    it has none: the task track. A task with no track (claimed before tracks
    existed) is untracked work."""
    track = t.track or policy.untracked
    return track if track in policy.tracks else ""


def launch_entries(t: TaskState, policy: ModelPolicy, order: Order,
                   stage: str = "") -> tuple[Entry, ...]:
    """The entries t's next launch walks, in the order they are tried: the
    launch track's list as models.candidates arranges it, the one provider
    that ran implement moved back for review. Empty when the launch has no
    usable track. The one definition: the dispatcher launches the first
    admitted entry, the status line names the first, the console offers all.
    `stage`: the runtime stage, for a launch the persisted state does not
    name yet (the stage that follows one just done); else next_stage."""
    track = launch_track(t, policy)
    if not track:
        return ()
    stage = stage or next_stage(t)
    return candidates(policy, track, stage,
                      review_avoid(t.implement_providers, stage), order=order)


@dataclass(frozen=True)
class StageSignal:
    stage: str
    status: str  # working | awaiting-review | done | blocked | awaiting-ci
    note: str = ""
    artifact: str = ""
    run_id: int = 0
    loop: str = ""    # bounded loop a working session is in: review | gate
    round: int = 0    # 1-based round of that loop
    track: str = ""   # spec done: the track for plan/implement/review; plan done may rename it
    # Plan ready report: the question blocks the session counted in its
    # review page. None = missing or not a non-negative integer.
    open_questions: int | None = None
    # Initial exploration proposal only; the controller validates membership
    # and provisions it at the spec -> plan boundary. Never a late expansion.
    repositories: tuple[str, ...] = ()


# The "waiting for a free slot" marker, wake-blocked-<target>-<issue> in
# state_dir. Written by dispatcher.main, read by web.sources.
WAKE_BLOCKED_PREFIX = "wake-blocked-"


@dataclass(frozen=True)
class TaskRepository:
    """One required repository of a task bundle: its owned checkout and branch."""
    repo: str          # "owner/name", a member of the task's project
    worktree: str
    branch: str


@dataclass(frozen=True)
class TaskBundle:
    """The controller-recorded repository scope of one task. `source` is where
    the scope came from ("definition" | "exploration"); `spec_repo` is the member
    repository holding the task's spec/plan ("" = not yet selected); a widened
    scope is a new `scope_revision`."""
    repositories: tuple[TaskRepository, ...]
    scope_revision: int = 1
    source: str = "definition"
    spec_repo: str = ""

    @property
    def awaits_spec_repo(self) -> bool:
        """Several repositories and none chosen for the spec/plan: a sole
        repository owns them."""
        return not self.spec_repo and len(self.repositories) > 1


class ScopeRefused(Exception):
    """A repository scope or spec repository outside the task's project, or a
    widening nobody confirmed."""


def _bundle_path(state_dir: str | Path, target: str, issue: int, repo: str) -> Path:
    return Path(state_dir) / f"bundle-{task_key(target, issue, repo)}.json"


def save_bundle(state_dir: str | Path, target: str, issue: int, bundle: TaskBundle, *,
                repo: str = "") -> None:
    write_json_atomic(_bundle_path(state_dir, target, issue, repo), asdict(bundle))


def read_bundle(state_dir: str | Path, target: str, issue: int, *,
                repo: str = "") -> TaskBundle | None:
    """The task's recorded scope, or None when it runs on its origin repository alone."""
    try:
        raw = json.loads(_bundle_path(state_dir, target, issue, repo).read_text())
    except FileNotFoundError:
        return None
    return TaskBundle(**{**raw, "repositories": tuple(
        TaskRepository(**r) for r in raw["repositories"])})


def save_pending_spec_repo(state_dir, target, issue, spec_repo, *, repo="") -> None:
    """Remember definition ownership while exploration has not established scope."""
    path = Path(state_dir) / f"spec-owner-{task_key(target, issue, repo)}.json"
    write_json_atomic(path, {"spec_repo": spec_repo})


def pending_spec_repo(state_dir, target, issue, *, repo="") -> str:
    path = Path(state_dir) / f"spec-owner-{task_key(target, issue, repo)}.json"
    try:
        return json.loads(path.read_text())["spec_repo"]
    except FileNotFoundError:
        return ""


def _recorded(state_dir, target, issue, repo) -> TaskBundle:
    bundle = read_bundle(state_dir, target, issue, repo=repo)
    if bundle is None:
        raise ScopeRefused(f"{target}#{issue} has no recorded repository scope")
    return bundle


def select_spec_repo(state_dir: str | Path, target: str, issue: int, spec_repo: str, *,
                     repo: str = "", members: Iterable[str]) -> TaskBundle:
    """Record the operator's spec/plan repository choice; ScopeRefused outside `members`."""
    bundle = _recorded(state_dir, target, issue, repo)
    if spec_repo not in set(members) or spec_repo not in {r.repo for r in bundle.repositories}:
        raise ScopeRefused(f"{spec_repo!r} is not a repository of this task's project scope")
    bundle = replace(bundle, spec_repo=spec_repo)
    save_bundle(state_dir, target, issue, bundle, repo=repo)
    return bundle


def confirm_scope(state_dir: str | Path, target: str, issue: int, add: Iterable[TaskRepository], *,
                  repo: str = "", members: Iterable[str]) -> TaskBundle:
    """Operator confirmation of a wider scope: a new scope revision."""
    bundle, add = _recorded(state_dir, target, issue, repo), tuple(add)
    if not {r.repo for r in add} <= set(members):
        raise ScopeRefused("a repository outside the project cannot join the task scope")
    held = {r.repo for r in bundle.repositories}
    bundle = replace(bundle, scope_revision=bundle.scope_revision + 1, repositories=(
        *bundle.repositories, *(r for r in add if r.repo not in held)))
    save_bundle(state_dir, target, issue, bundle, repo=repo)
    return bundle


def task_key(target: str, issue: int, repo: str = "") -> str:
    """`<target>-<issue>`, or `<owner>~<name>~<target>-<issue>` when the task's
    repository is proven: the key every per-task file in state_dir is named
    by. Two repositories of one target never share a key."""
    key = f"{target}-{issue}"
    return f"{repo.replace('/', '~')}~{key}" if repo else key


def parse_task_key(name: str) -> tuple[str, int] | None:
    """(target, issue) from a `<target>-<issue>` key, else None (including
    a legacy bare `<issue>`). rpartition, since target names may themselves
    contain '-'. A repository-qualified key is not a legacy one: None."""
    if "~" in name:
        return None
    target, _, issue = name.rpartition("-")
    return (target, int(issue)) if target and issue.isdigit() else None


def parse_qualified_task_key(name: str) -> tuple[str, str, int] | None:
    """(repo, target, issue) from an `<owner>~<name>~<target>-<issue>` key,
    else None: the inverse of task_key for a proven repository."""
    owner, sep1, rest = name.partition("~")
    repo_name, sep2, tail = rest.partition("~")
    parsed = parse_task_key(tail) if sep1 and sep2 else None
    return (f"{owner}/{repo_name}", *parsed) if parsed else None


def _path(state_dir: str | Path, target: str, issue: int, repo: str = "") -> Path:
    return Path(state_dir) / f"task-{task_key(target, issue, repo)}.json"


def _legacy_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"task-{issue}.json"


def _previous(state_dir: str | Path, ts: TaskState) -> TaskState | None:
    """The state a save replaces; None also when the file cannot be read:
    the save is what repairs it."""
    try:
        return load(state_dir, ts.target, ts.issue, repo=ts.repo)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save(state_dir: str | Path, ts: TaskState) -> None:
    previous = _previous(state_dir, ts)
    if ts.stage in TERMINAL_STAGES:
        stamp = (previous.terminal_at
                 if previous and previous.stage in TERMINAL_STAGES else ts.updated_at)
        ts = replace(ts, terminal_at=stamp)
    else:
        ts = replace(ts, terminal_at="")
    _write_task(_path(state_dir, ts.target, ts.issue, ts.repo), ts)
    # Lazy migration: the first save under the new key retires the legacy twin.
    # A qualified task owns no legacy file: that would be a guess.
    if not ts.repo:
        _retire_legacy(state_dir, ts.target, ts.issue)


def _retire_legacy(state_dir: str | Path, target: str, issue: int) -> None:
    """Unlink task-{issue}.json only when it speaks for `target`; another
    target's task with the same issue number must survive."""
    legacy = _read(_legacy_path(state_dir, issue))
    if legacy is not None and legacy.target == target:
        _legacy_path(state_dir, issue).unlink(missing_ok=True)


def write_json_atomic(path: Path, doc: dict) -> None:
    """For a state file the web process and the dispatcher share (the usage
    cache, the priority mode): write a sibling temp file of its own and rename
    it over, so a reader never sees a torn one and two writers never share a
    temp file. mkstemp creates 0600 and the two units may run as different
    users, so the file is opened up to 0644 before the rename. A failed write
    or rename leaves no temp file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_task(p: Path, ts: TaskState) -> None:
    d = asdict(ts)
    d["stage"] = ts.stage.value
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


# The next launch of a task at one of these is a PR feedback round.
_PR_OPEN_STAGES = frozenset({Stage.PR_OPEN, Stage.ADDRESS_REVIEW})
_PAST_IMPLEMENT = _PR_OPEN_STAGES | {Stage.REVIEW}


def _effective_stage(d: dict) -> Stage:
    """A crashed task counts as the stage it resumes."""
    if d["stage"] is Stage.FAILED and d.get("crashed_stage"):
        return Stage(d["crashed_stage"])
    return d["stage"]


def _migrate_implement_pick(d: dict) -> None:
    """State from before this change. No recorded provider: the implement
    pick's provider ran implement. Past implement the implement pick is no
    the implement session's any more: it becomes the feedback pick when the PR is open and
    there is none yet, so the next feedback round stays on its provider, and
    is dropped otherwise. A crashed task counts as the stage it resumes.
    Afterwards there is a provider and no such pick, so a second read (or a
    read after a save) changes nothing."""
    picks = d["picks"]
    if not d.get("implement_providers"):
        d["implement_providers"] = list(
            filter(None, [pick_provider(picks, Stage.IMPLEMENT.value)]))
    stage = _effective_stage(d)
    if stage not in _PAST_IMPLEMENT or IMPLEMENT_PICK not in picks:
        return
    pick = picks.pop(IMPLEMENT_PICK)
    if stage in _PR_OPEN_STAGES:
        picks.setdefault(FEEDBACK_PICK, pick)


def _read(p: Path) -> TaskState | None:
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    d["stage"] = Stage(d["stage"])
    if d["stage"] in TERMINAL_STAGES:
        d["terminal_at"] = d.get("terminal_at") or d.get("done_at") or d["updated_at"]
    else:
        d["terminal_at"] = ""
    d["labels"] = tuple(d.get("labels", ()))
    d["picks"] = dict(d.get("picks") or {})
    _migrate_implement_pick(d)
    d.pop("pending_reply", None)   # retired field, see original comment
    if d.get("operator_request") is not None:
        raw = d["operator_request"]
        kind = raw.get("kind")
        if kind == "plan-approval":
            d["operator_request"] = PlanApprovalRequest(
                path=raw.get("path", ""), fingerprint=raw.get("fingerprint", ""))
        elif kind == "answers":
            d["operator_request"] = AnswersRequest(
                path=raw.get("path", ""), fingerprint=raw.get("fingerprint", ""))
        else:
            raise ValueError(f"unrecognized operator_request kind {kind!r}")
    d["repair_prs"] = tuple(d.get("repair_prs") or ())
    d["dropped_pr_repos"] = tuple(d.get("dropped_pr_repos") or ())
    d["required_prs"] = tuple(RequiredPR(**r) for r in d.get("required_prs") or ())
    d.pop("artifact", None)        # retired field (slice 24)
    # Retired fields: one implement session, no cursor and no ticket tracks.
    for retired in ("ticket_cursor", "ticket_tracks", "ticket_names",
                    "ticket_without_pick"):
        d.pop(retired, None)
    return TaskState(**d)


def load(state_dir: str | Path, target: str, issue: int, *,
         repo: str = "") -> TaskState | None:
    ts = _read(_path(state_dir, target, issue, repo))
    if ts is not None or repo:
        return ts
    legacy = _read(_legacy_path(state_dir, issue))
    # A legacy file speaks only for its own target.
    return legacy if legacy is not None and legacy.target == target else None


def load_all(state_dir: str | Path) -> list[TaskState]:
    """Every task on disk. A single corrupt file (truncated write, unknown
    field after a partial upgrade) is skipped rather than taking the whole
    scan down — callers like _prune_snapshots and capacity/slot accounting
    need every OTHER task's state to stay visible. A directly-addressed file
    (load()) still raises: silently losing one task from a targeted lookup
    is worse than a loud crash, but silently losing it from a scan that
    every other task depends on (allocate_slot, active()) is worse still."""
    root = Path(state_dir)
    if not root.exists():
        return []
    tasks = []
    for p in root.glob("task-*.json"):
        try:
            ts = _read(p)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        # Identity comes from the JSON, never the filename stem.
        if ts is not None:
            tasks.append(ts)
    return sorted(tasks, key=lambda t: (t.target, t.issue, t.repo))


def delete(state_dir: str | Path, target: str, issue: int, *, repo: str = "") -> None:
    _path(state_dir, target, issue, repo).unlink(missing_ok=True)
    if not repo:
        _retire_legacy(state_dir, target, issue)


def max_slots(capacity: int) -> int:
    """Slots are a resource NAMESPACE (ports 8100+slot / 5200+slot and the
    {slot} substitution in verify_cmd), not a concurrency limit — capacity is
    the concurrency limit. Two spare slots above capacity give a resume room
    to grab a fresh number while another session is mid-teardown, and deriving
    the ceiling means raising capacity: in targets.yaml can never silently
    reintroduce the starvation this replaced."""
    return capacity + 2


def holds_slot(t: TaskState) -> bool:
    """Does this task legitimately still own its slot? Every park that ENDS
    the session gives the slot back; PARK_LOGIN is the only one that keeps a
    live pane, so it is the only park that keeps a slot."""
    return (t.stage in IN_FLIGHT_STAGES
            and (not t.park or t.park == PARK_LOGIN))


def allocate_slot(existing: list[TaskState], max_slots: int,
                  reserved: Iterable[int] = ()) -> int | None:
    """`reserved`: the slots of owners that are not tasks (reserved_slots())."""
    held = {t.slot for t in existing if holds_slot(t) and t.slot != NO_SLOT}
    held.update(reserved)
    for slot in range(max_slots):
        if slot not in held:
            return slot
    return None


ALLOCATIONS_FILE = "allocations.json"


@dataclass(frozen=True)
class Allocation:
    """One execution slot and who holds it. owner_kind: task | triage |
    verification."""

    owner_kind: str
    owner_id: str
    slot: int


def _stored_allocations(state_dir: str | Path) -> list[Allocation]:
    """The holds of owners that are not unparked tasks: a verification, a
    triage sweep, a task whose containers are not yet confirmed stopped."""
    try:
        rows = json.loads((Path(state_dir) / ALLOCATIONS_FILE).read_text())
        return [Allocation(**row) for row in rows]
    except (OSError, ValueError, TypeError):
        return []


def hold(state_dir: str | Path, kind: str, owner_id: str, slot: int) -> None:
    kept = [a for a in _stored_allocations(state_dir)
            if (a.owner_kind, a.owner_id) != (kind, owner_id)]
    _write_allocations(state_dir, [*kept, Allocation(kind, owner_id, slot)])


def release(state_dir: str | Path, kind: str, owner_id: str) -> None:
    stored = _stored_allocations(state_dir)
    kept = [a for a in stored if (a.owner_kind, a.owner_id) != (kind, owner_id)]
    if len(kept) != len(stored):
        _write_allocations(state_dir, kept)


def _write_allocations(state_dir: str | Path, rows: list[Allocation]) -> None:
    path = Path(state_dir) / ALLOCATIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
    with os.fdopen(fd, "w") as stream:
        json.dump([asdict(a) for a in rows], stream)
    os.replace(temporary, path)


def allocations(state_dir: str | Path) -> list[Allocation]:
    """The one execution allocation view: every slot held by a task, a triage
    sweep or an onboarding verification. Slots are unique across owners."""
    tasks = [Allocation("task", task_key(t.target, t.issue, t.repo), t.slot)
             for t in load_all(state_dir) if holds_slot(t) and t.slot != NO_SLOT]
    return [*tasks, *_stored_allocations(state_dir)]


def reserved_slots(state_dir: str | Path) -> set[int]:
    return {a.slot for a in _stored_allocations(state_dir) if a.slot != NO_SLOT}


def held_count(state_dir: str | Path) -> int:
    """Capacity units held by verifications and tasks still stopping. Tasks
    that run are counted by active(), a running triage sweep by its tab."""
    return sum(a.owner_kind != "triage" for a in _stored_allocations(state_dir))


def _open_questions(raw: object) -> int | None:
    """Tolerant on purpose: a bad count must not make the signal unreadable.
    `type() is int` keeps bool out (True is an int in Python)."""
    return raw if type(raw) is int and raw >= 0 else None


STAGE_SIGNAL_MAX_BYTES = 64 * 1024
_SIGNAL_INT_MAX = 2 ** 53   # what JSON carries exactly; far above any run id


def _signal_int(d: dict, key: str) -> int:
    """A number field of the signal: 0 when left out or null, else an integer
    in range, also written as a string of digits (sessions do that). Anything
    else (a float, also 1e400; a bool; other text) raises ValueError: the
    signal is unreadable, as for any other malformed one."""
    raw = d.get(key)
    if raw is None or raw == "":
        return 0
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 16:
        raw = int(raw)
    if type(raw) is not int or not 0 <= raw <= _SIGNAL_INT_MAX:
        raise ValueError(f"{key} is not a sane integer: {raw!r}")
    return raw


def read_regular(path: str | Path, max_bytes: int) -> bytes | None:
    """The bytes of a regular file a session wrote; None for anything else:
    missing, unreadable, a symlink (not followed), a FIFO or a device (the
    open never blocks), or larger than max_bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            raw = f.read(max_bytes + 1)
    except (OSError, ValueError):   # ValueError: a NUL character in the path
        return None
    return raw if len(raw) <= max_bytes else None


def read_stage_signal(worktree: str | Path) -> StageSignal | None:
    """The session-written .agent/stage.json, read defensively for the
    dispatcher and the console alike: whatever a model put at that path, the
    answer is a signal or None, never a hang or an exception."""
    raw = read_regular(Path(worktree) / ".agent" / "stage.json", STAGE_SIGNAL_MAX_BYTES)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        note = d.get("note", "")
        repositories = d.get("repositories", [])
        if not isinstance(repositories, list) or not all(
                isinstance(repo, str) and repo for repo in repositories):
            raise ValueError("repositories must be an array of repository names")
        return StageSignal(
            stage=str(d["stage"]),
            status=str(d["status"]),
            note=note if isinstance(note, str) else "",
            artifact=str(d.get("artifact", "")),
            run_id=_signal_int(d, "run_id"),
            loop=str(d.get("loop", "") or ""),
            round=_signal_int(d, "round"),
            track=str(d.get("track", "") or ""),
            open_questions=_open_questions(d.get("open_questions")),
            repositories=tuple(repositories),
        )
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError,
            OverflowError):
        return None


def consumes_capacity(t: TaskState) -> bool:
    """One task's answer to "does this hold a capacity unit?" — see active()."""
    return t.stage in IN_FLIGHT_STAGES and (not t.park or t.park == PARK_LOGIN)


def active(tasks: list[TaskState]) -> list[TaskState]:
    """Tasks consuming capacity: unparked ones, plus login-parked ones —
    "parked ⇒ container stopped" holds for every park except PARK_LOGIN,
    whose whole point is a session left running at the /login prompt.
    Counting it frees the dispatcher from claiming new work during a
    box-wide auth expiry, when every fresh session would hit the same
    prompt."""
    return [t for t in tasks if consumes_capacity(t)]


def parked(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if t.stage in IN_FLIGHT_STAGES and t.park]


@dataclass(frozen=True)
class SessionRecord:
    """The conversation recorded for a task's continued stage."""

    session_id: str
    stage: str


def _session_path(state_dir: str | Path, target: str, issue: int, repo: str = "") -> Path:
    return Path(state_dir) / f"session-{task_key(target, issue, repo)}"


def write_session(state_dir: str | Path, target: str, issue: int,
                  record: SessionRecord, *, repo: str = "") -> None:
    """Replace a task's conversation atomically; only waitd writes it."""
    write_json_atomic(_session_path(state_dir, target, issue, repo), asdict(record))


def read_session(state_dir: str | Path, target: str, issue: int, *,
                 repo: str = "") -> SessionRecord | None:
    """Read a task's conversation; missing or malformed records are absent."""
    try:
        d = json.loads(_session_path(state_dir, target, issue, repo).read_text())
        session_id, stage = d["session_id"], d["stage"]
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(stage, str) or not stage:
            return None
        return SessionRecord(session_id, stage)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def clear_session(state_dir: str | Path, target: str, issue: int, *,
                  repo: str = "") -> None:
    """Remove only the task's conversation; a missing record is harmless."""
    _session_path(state_dir, target, issue, repo).unlink(missing_ok=True)


def _waiting_path(state_dir: str | Path, target: str, issue: int, repo: str = "") -> Path:
    return Path(state_dir) / f"waiting-{task_key(target, issue, repo)}"


def _legacy_waiting_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{issue}"


def mark_waiting(state_dir: str | Path, target: str, issue: int, *,
                 repo: str = "") -> None:
    p = _waiting_path(state_dir, target, issue, repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    _background_path(state_dir, target, issue, repo).unlink(missing_ok=True)  # latest turn end wins


@dataclass(frozen=True)
class BackgroundWait:
    tasks: tuple[str, ...]   # identities the latest report named
    since: float             # cap clock: restarts when a report names new work
    reported: float          # time of the latest report


def _background_path(state_dir: str | Path, target: str, issue: int, repo: str = "") -> Path:
    return Path(state_dir) / f"background-{task_key(target, issue, repo)}"


def _identity(entry) -> str:
    if isinstance(entry, dict) and entry.get("id") is not None:
        return str(entry["id"])
    return json.dumps(entry, sort_keys=True)


def read_background(state_dir: str | Path, target: str, issue: int, *,
                    repo: str = "") -> BackgroundWait | None:
    try:
        d = json.loads(_background_path(state_dir, target, issue, repo).read_text())
        return BackgroundWait(tuple(d["tasks"]), float(d["since"]), float(d["reported"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mark_background(state_dir: str | Path, target: str, issue: int,
                    tasks: list, now: float | None = None, *, repo: str = "") -> None:
    now = time.time() if now is None else now
    ids = tuple(_identity(t) for t in tasks)
    old = read_background(state_dir, target, issue, repo=repo)
    since = old.since if old and set(ids) <= set(old.tasks) else now
    p = _background_path(state_dir, target, issue, repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    _waiting_path(state_dir, target, issue, repo).unlink(missing_ok=True)  # latest turn end wins; before the write so a pass never sees both
    tmp = p.with_name(p.name + ".tmp")  # with_suffix would truncate a dotted target
    tmp.write_text(json.dumps({"tasks": list(ids), "since": since, "reported": now}))
    tmp.replace(p)


def has_waiting(state_dir: str | Path, target: str, issue: int, *,
                repo: str = "") -> bool:
    return (_waiting_path(state_dir, target, issue, repo).exists()
            or (not repo and _legacy_waiting_path(state_dir, issue).exists()))


def clear_turn_markers(state_dir: str | Path, target: str, issue: int, *,
                       repo: str = "") -> None:
    """Remove every turn-end marker (waiting, legacy waiting, background).
    The only way a background marker goes besides a waiting ping: every
    session end or replacement calls it, so a new session never inherits an
    old clock."""
    _waiting_path(state_dir, target, issue, repo).unlink(missing_ok=True)
    if not repo:
        _legacy_waiting_path(state_dir, issue).unlink(missing_ok=True)
    _background_path(state_dir, target, issue, repo).unlink(missing_ok=True)


def archive_root(state_dir: str | Path, target: str, issue: int, *,
                 repo: str = "") -> Path:
    """Task archive namespace, shared by its snapshot and retained artifacts."""
    ident = f"{repo}\0{target}\0{issue}" if repo else f"{target}\0{issue}"
    key = hashlib.sha256(ident.encode()).hexdigest()
    return Path(state_dir) / "artifacts" / key


def archive(state_dir: str | Path, task: TaskState) -> None:
    if task.stage in TERMINAL_STAGES:
        _write_task(archive_root(state_dir, task.target, task.issue,
                                 repo=task.repo) / "task.json", task)


def load_archived(state_dir: str | Path, target: str, issue: int, *,
                  repo: str = "") -> TaskState | None:
    task = _read(archive_root(state_dir, target, issue, repo=repo) / "task.json")
    return task if task and task.stage in TERMINAL_STAGES else None


def archived_named(state_dir: str | Path, target: str, issue: int) -> list[TaskState]:
    """Every archived task of this target and number, whatever its repository:
    what an old, repository-less reference may name."""
    found = []
    for path in (Path(state_dir) / "artifacts").glob("*/task.json"):
        try:
            task = _read(path)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if task and task.stage in TERMINAL_STAGES and (task.target, task.issue) == (target, issue):
            found.append(task)
    return found


def _unqualified(paths) -> set[tuple[str, int]]:
    found = set()
    for p in paths:
        try:
            ts = _read(p)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if ts is not None and not ts.repo:
            found.add((ts.target, ts.issue))
    return found


_SIDECAR_GLOBS = (("session-*", "session-", ""), ("waiting-*", "waiting-", ""),
                  ("background-*", "background-", ""),
                  ("wake-blocked-*", WAKE_BLOCKED_PREFIX, ""),
                  # directory names of execution_overrides.DIR and failures.quarantine_path
                  ("execution-overrides/*.json", "", ".json"),
                  ("quarantine/*.json", "", ".json"),
                  ("messages/*.jsonl", "", ".jsonl"))


def _legacy_identities(root: Path, proven: dict[str, str]) -> set[tuple[str, int]]:
    """(target, issue) of every unqualified record of a proven target."""
    found = _unqualified(root.glob("task-*.json"))
    found |= _sidecar_identities(root)
    found |= _unqualified(root.glob("artifacts/*/task.json"))
    found |= _unqualified_intents(root)
    return {(t, i) for t, i in found if t in proven}


def _sidecar_identities(root: Path) -> set[tuple[str, int]]:
    """(target, issue) parsed from the name of every sidecar file."""
    names = [p.name.removeprefix(pre).removesuffix(suf)
             for pattern, pre, suf in _SIDECAR_GLOBS for p in root.glob(pattern)]
    return {k for k in map(parse_task_key, names) if k}


def _unqualified_intents(root: Path) -> set[tuple[str, int]]:
    found = set()
    for p in (root / "intents").glob("*.json"):
        try:
            rec = json.loads(p.read_text())
            if not rec.get("repo"):
                found.add((rec["target"], int(rec["issue"])))
        except (OSError, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            continue
    return found


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _task_sources(root: Path, target: str, issue: int) -> list[Path]:
    """The unqualified task files of one identity: the target-keyed one and
    the issue-only one load() still reads (which speaks only for its target)."""
    found = []
    for p in (_path(root, target, issue), _legacy_path(root, issue)):
        try:
            ts = _read(p)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if ts is not None and not ts.repo and ts.target == target:
            found.append(p)
    return found


def _read_messages(p: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in p.read_text().splitlines():
        try:
            d = json.loads(raw)
            out.setdefault(str(d["id"]), d)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue   # same as messages._parse: a malformed line is dropped
    return out


def _merge_messages(src: Path, dst: Path) -> None:
    """Fold a legacy queue into the qualified one: every id of both survives,
    and a message delivered in either stays delivered."""
    merged = _read_messages(dst)
    for mid, d in _read_messages(src).items():
        if mid not in merged:
            merged[mid] = d
        elif d.get("delivered_at") and not merged[mid].get("delivered_at"):
            merged[mid]["delivered_at"] = d["delivered_at"]
    rows = sorted(merged.values(), key=lambda d: str(d.get("created_at", "")))
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text("".join(json.dumps(d) + "\n" for d in rows))
    os.replace(tmp, dst)
    _verify_messages(src, dst)


def _verify_messages(src: Path, dst: Path) -> None:
    """Verify every source id and its delivery status survived the merge."""
    after = _read_messages(dst)
    if any(mid not in after or (d.get("delivered_at") and not after[mid].get("delivered_at"))
           for mid, d in _read_messages(src).items()):
        raise OSError(f"migration of {src} not verified")


def _migrate_intents(root: Path, target: str, issue: int, repo: str) -> None:
    """Hand each pending unqualified intent of the task to the repository."""
    for p in sorted((root / "intents").glob("*.json")):
        try:
            rec = json.loads(p.read_text())
            owned = (rec["target"] == target and int(rec["issue"]) == issue
                     and not rec.get("repo"))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if owned:
            write_json_atomic(p, {**rec, "repo": repo})


class _MigrationConflict(Exception):
    """Both copies survive until an operator resolves ambiguous ownership/content."""


def _migration_bytes(path: Path, identity: tuple[str, int, str] | None) -> bytes:
    if identity is None:
        return path.read_bytes()
    target, issue, repo = identity
    try:
        task = _read(path)
        if task is None or (task.target, task.issue) != (target, issue):
            raise ValueError("wrong task identity")
        if task.repo not in ("", repo):
            raise ValueError("wrong repository")
        return json.dumps(asdict(replace(task, repo=repo)), sort_keys=True).encode()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise _MigrationConflict(f"invalid task replacement {path}: {exc}") from exc


def _reconcile_copy(root: Path, src: Path, dst: Path,
                    identity: tuple[str, int, str] | None = None) -> None:
    """Three-way copy: only replace a destination known to be unchanged.

    The journal records the last agreed source version. A destination-only
    update wins; a source-only update is copied; concurrent edits are retained
    in place for explicit resolution. No timestamps or existence-based guesses.
    """
    journal = root / "migration-copies.json"
    versions = json.loads(journal.read_text()) if journal.exists() else {}
    key = str(src.relative_to(root)) + " -> " + str(dst.relative_to(root))
    source = _migration_bytes(src, identity)
    digest = hashlib.sha256(source).hexdigest()
    if _destination_wins(src, dst, identity, digest, versions.get(key)):
        return  # the source was copied before the destination changed
    _write_copy(src, dst, identity, source)
    versions[key] = digest
    write_json_atomic(journal, versions)


def _destination_wins(src: Path, dst: Path, identity: tuple[str, int, str] | None,
                      digest: str, previous: str | None) -> bool:
    """True when only the destination changed since the last agreed copy;
    raise when both sides changed or the destination's owner is unverified."""
    if not dst.exists():
        return False
    if identity is not None and _read_repository(dst) != identity[2]:
        raise _MigrationConflict(f"unverified repository ownership: {dst}")
    current = hashlib.sha256(_migration_bytes(dst, identity)).hexdigest()
    if current == digest:
        return False
    if digest == previous:
        return True
    if current != previous:
        raise _MigrationConflict(f"conflicting migration copies: {src} and {dst}")
    return False


def _write_copy(src: Path, dst: Path, identity: tuple[str, int, str] | None,
                source: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if identity is None:
        _copy_file(src, dst)
    else:
        write_json_atomic(dst, json.loads(source))
    if _migration_bytes(dst, identity) != source:
        raise OSError(f"migration of {src} not verified")


def _read_repository(path: Path) -> str:
    try:
        return json.loads(path.read_text()).get("repo", "")
    except (json.JSONDecodeError, AttributeError):
        return ""


def _migrate_files(root: Path, target: str, issue: int, repo: str) -> list[Path]:
    """Reconcile task and sidecars before retiring any original."""
    originals = _task_sources(root, target, issue)
    dst = _path(root, target, issue, repo)
    for src in originals:
        _reconcile_copy(root, src, dst, (target, issue, repo))
    pairs = zip(_sidecar_paths(root, target, issue),
                _sidecar_paths(root, target, issue, repo))
    for src, new in pairs:
        if src.exists():
            _reconcile_copy(root, src, new)
            originals.append(src)
    # waiting-<issue> speaks for the bare task-<issue>.json; move it only with that record
    legacy_waiting = _legacy_waiting_path(root, issue)
    if _legacy_path(root, issue) in originals and legacy_waiting.exists():
        _reconcile_copy(root, legacy_waiting, _waiting_path(root, target, issue, repo))
        originals.append(legacy_waiting)
    originals += _migrate_messages(root, target, issue, repo)
    _migrate_intents(root, target, issue, repo)
    return originals


def _migrate_messages(root: Path, target: str, issue: int, repo: str) -> list[Path]:
    """Copy or merge the legacy queue; return its original for deferred removal."""
    src = root / "messages" / f"{task_key(target, issue)}.jsonl"
    if not src.exists():
        return []
    dst = root / "messages" / f"{task_key(target, issue, repo)}.jsonl"
    if dst.exists():
        _merge_messages(src, dst)
    else:
        _copy_file(src, dst)
    return [src]


def _sidecar_paths(root: Path, target: str, issue: int, repo: str = "") -> list[Path]:
    k = task_key(target, issue, repo)
    return [_session_path(root, target, issue, repo), _waiting_path(root, target, issue, repo),
            _background_path(root, target, issue, repo),
            root / "execution-overrides" / f"{k}.json", root / "quarantine" / f"{k}.json",
            root / f"{WAKE_BLOCKED_PREFIX}{k}"]


def _verify_tree(old_root: Path, new_root: Path) -> None:
    if {p.relative_to(old_root) for p in old_root.rglob("*")} \
            - {p.relative_to(new_root) for p in new_root.rglob("*")}:
        raise OSError(f"migration of {old_root} not verified")


def _migrate_archive(root: Path, target: str, issue: int, repo: str) -> list[Path]:
    old_root = archive_root(root, target, issue)
    if not old_root.is_dir():
        return []
    new_root = archive_root(root, target, issue, repo=repo)

    def copy(src, dst):
        src, dst = Path(src), Path(dst)
        identity = (target, issue, repo) if src == old_root / "task.json" else None
        _reconcile_copy(root, src, dst, identity)
        return str(dst)

    shutil.copytree(old_root, new_root, dirs_exist_ok=True, copy_function=copy)
    _verify_tree(old_root, new_root)
    return [old_root]


def migrate_legacy(state_dir: str | Path, proven: dict[str, str]) -> None:
    """Migrate proven identities, retaining originals on ambiguous conflicts.

    Copies are journaled and reconciled on retries. Original records remain
    until every replacement has been verified; message queues merge by id.
    """
    root = Path(state_dir)
    originals: list[Path] = []
    for target, issue in sorted(_legacy_identities(root, proven)):
        repo = proven[target]
        try:
            pending = _migrate_files(root, target, issue, repo)
            pending += _migrate_archive(root, target, issue, repo)
        except _MigrationConflict as exc:
            warnings.warn(f"migration unresolved for {target}#{issue}: {exc}",
                          RuntimeWarning)
            continue
        originals += pending
    for src in originals:
        shutil.rmtree(src) if src.is_dir() else src.unlink()


def session_owner(state_dir: str | Path, target: str, issue: int) -> str:
    """The repository a launch record names as owner of the shared session
    name; "" when no record exists."""
    try:
        return session_owner_path(state_dir, target, issue).read_text()
    except (OSError, ValueError):
        return ""


def owns_task_session(state_dir: str | Path, target: str, issue: int, repo: str) -> bool:
    """Only a launch record proves which repository owns a shared session name.

    Task inventories can shrink and are not evidence of session ownership.
    Empty repositories retain the pre-qualification session contract.
    """
    if not repo:
        return True
    return session_owner(state_dir, target, issue) == repo


def session_owner_path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"session-owner-{task_key(target, issue)}"


def record_session_owner(state_dir: str | Path, target: str, issue: int, repo: str) -> None:
    path = session_owner_path(state_dir, target, issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repo)


def _token_path(state_dir: str | Path, target: str, issue: int, repo: str) -> Path:
    return Path(state_dir) / "execution-tokens" / task_key(target, issue, repo)


def issue_execution_token(state_dir: str | Path, target: str, issue: int,
                          repo: str) -> str:
    """A fresh secret for one launch of one repository's execution. Only that
    execution's worktree holds it, so its Stop ping can prove which
    execution sent it; a sibling's session cannot read it."""
    path = _token_path(state_dir, target, issue, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    path.write_text(token)
    path.chmod(0o600)
    return token


def token_issued(state_dir: str | Path, target: str, issue: int, repo: str) -> bool:
    """True once this repository's execution was launched with a token: its
    hook then carries one, so a tokenless ping cannot speak for it."""
    return _token_path(state_dir, target, issue, repo).exists()


def execution_token_matches(state_dir: str | Path, target: str, issue: int,
                            repo: str, presented: object) -> bool:
    """True when `presented` is the token issued for this identity. A missing
    token file never matches: a declared repository must prove itself."""
    try:
        issued = _token_path(state_dir, target, issue, repo).read_text()
    except OSError:
        return False
    return isinstance(presented, str) and secrets.compare_digest(issued, presented)
