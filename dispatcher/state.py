"""Per-task runtime state (~/agent-ops-state/task-<target>-<issue>.json) and the
stage.json signal sessions write into their worktree."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import NamedTuple

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
    path: str   # the plan session's review summary, worktree-relative
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
    # None=no request; PlanApprovalRequest while at gate; AnswersRequest written
    # ONLY by _park_for_input in dispatcher/main.py, and only when a
    # worktree-contained path resolves — so an answers request never exists
    # without a valid path.
    operator_request: "OperatorRequest | None" = None


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
    # Plan ready report: the open questions the session counted in its
    # summary. None = missing or not a non-negative integer.
    open_questions: int | None = None


# The "waiting for a free slot" marker, wake-blocked-<target>-<issue> in
# state_dir. Written by dispatcher.main, read by web.sources.
WAKE_BLOCKED_PREFIX = "wake-blocked-"


def task_key(target: str, issue: int) -> str:
    """`<target>-<issue>`: the (target, issue) key every per-task file in
    state_dir is named by."""
    return f"{target}-{issue}"


def parse_task_key(name: str) -> tuple[str, int] | None:
    """(target, issue) from a `<target>-<issue>` key, else None (including
    a legacy bare `<issue>`). rpartition, since target names may themselves
    contain '-'."""
    target, _, issue = name.rpartition("-")
    return (target, int(issue)) if target and issue.isdigit() else None


def _path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"task-{task_key(target, issue)}.json"


def _legacy_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"task-{issue}.json"


def _previous(state_dir: str | Path, ts: TaskState) -> TaskState | None:
    """The state a save replaces; None also when the file cannot be read:
    the save is what repairs it."""
    try:
        return load(state_dir, ts.target, ts.issue)
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
    _write_task(_path(state_dir, ts.target, ts.issue), ts)
    # Lazy migration: the first save under the new key retires the legacy twin.
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
            d["operator_request"] = AnswersRequest(path=raw.get("path", ""))
        else:
            raise ValueError(f"unrecognized operator_request kind {kind!r}")
    d.pop("artifact", None)        # retired field (slice 24)
    # Retired fields: one implement session, no cursor and no ticket tracks.
    for retired in ("ticket_cursor", "ticket_tracks", "ticket_names",
                    "ticket_without_pick"):
        d.pop(retired, None)
    return TaskState(**d)


def load(state_dir: str | Path, target: str, issue: int) -> TaskState | None:
    ts = _read(_path(state_dir, target, issue))
    if ts is not None:
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
    return sorted(tasks, key=lambda t: (t.target, t.issue))


def delete(state_dir: str | Path, target: str, issue: int) -> None:
    _path(state_dir, target, issue).unlink(missing_ok=True)
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


def allocate_slot(existing: list[TaskState], max_slots: int) -> int | None:
    held = {t.slot for t in existing if holds_slot(t) and t.slot != NO_SLOT}
    for slot in range(max_slots):
        if slot not in held:
            return slot
    return None


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


def _waiting_path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{task_key(target, issue)}"


def _legacy_waiting_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{issue}"


def mark_waiting(state_dir: str | Path, target: str, issue: int) -> None:
    p = _waiting_path(state_dir, target, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    _background_path(state_dir, target, issue).unlink(missing_ok=True)  # latest turn end wins


@dataclass(frozen=True)
class BackgroundWait:
    tasks: tuple[str, ...]   # identities the latest report named
    since: float             # cap clock: restarts when a report names new work
    reported: float          # time of the latest report


def _background_path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"background-{task_key(target, issue)}"


def _identity(entry) -> str:
    if isinstance(entry, dict) and entry.get("id") is not None:
        return str(entry["id"])
    return json.dumps(entry, sort_keys=True)


def read_background(state_dir: str | Path, target: str, issue: int) -> BackgroundWait | None:
    try:
        d = json.loads(_background_path(state_dir, target, issue).read_text())
        return BackgroundWait(tuple(d["tasks"]), float(d["since"]), float(d["reported"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mark_background(state_dir: str | Path, target: str, issue: int,
                    tasks: list, now: float | None = None) -> None:
    now = time.time() if now is None else now
    ids = tuple(_identity(t) for t in tasks)
    old = read_background(state_dir, target, issue)
    since = old.since if old and set(ids) <= set(old.tasks) else now
    p = _background_path(state_dir, target, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    _waiting_path(state_dir, target, issue).unlink(missing_ok=True)  # latest turn end wins; before the write so a pass never sees both
    tmp = p.with_name(p.name + ".tmp")  # with_suffix would truncate a dotted target
    tmp.write_text(json.dumps({"tasks": list(ids), "since": since, "reported": now}))
    tmp.replace(p)


def has_waiting(state_dir: str | Path, target: str, issue: int) -> bool:
    return (_waiting_path(state_dir, target, issue).exists()
            or _legacy_waiting_path(state_dir, issue).exists())


def clear_turn_markers(state_dir: str | Path, target: str, issue: int) -> None:
    """Remove every turn-end marker (waiting, legacy waiting, background).
    The only way a background marker goes besides a waiting ping: every
    session end or replacement calls it, so a new session never inherits an
    old clock."""
    _waiting_path(state_dir, target, issue).unlink(missing_ok=True)
    _legacy_waiting_path(state_dir, issue).unlink(missing_ok=True)
    _background_path(state_dir, target, issue).unlink(missing_ok=True)


def archive_root(state_dir: str | Path, target: str, issue: int) -> Path:
    """Task archive namespace, shared by its snapshot and retained artifacts."""
    key = hashlib.sha256(f"{target}\0{issue}".encode()).hexdigest()
    return Path(state_dir) / "artifacts" / key


def archive(state_dir: str | Path, task: TaskState) -> None:
    if task.stage in TERMINAL_STAGES:
        _write_task(archive_root(state_dir, task.target, task.issue) / "task.json", task)


def load_archived(state_dir: str | Path, target: str, issue: int) -> TaskState | None:
    task = _read(archive_root(state_dir, target, issue) / "task.json")
    return task if task and task.stage in TERMINAL_STAGES else None
