"""Per-task runtime state (~/agent-ops-state/task-<repository>-<issue>.json) and the
stage.json signal sessions write into their worktree."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from dispatcher import status as statuses
from dispatcher.models import (FEEDBACK_PICK, IMPLEMENT_PICK, Entry,
                               ModelPolicy, Order, candidates, pick_provider,
                               review_avoid)
from dispatcher.status import RUNNING, Cause, Kind, Status, ended, occupies


class Stage(str, Enum):
    """The pipeline position only: a task that fails, is canceled or is done
    keeps the stage it had (ADR 0014)."""
    QUEUED = "queued"
    SPEC = "spec"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"           # fresh-eyes review; rebases, verifies, opens the PR
    PR_OPEN = "pr-open"
    ADDRESS_REVIEW = "address-review"


def next_stage(t: "TaskState") -> str:
    """The runtime stage a task's next launch runs, the one answer the
    dispatcher, the status lines and the console share: its own stage, but a
    task at pr-open, which has no session of its own, launches an
    address-review round."""
    return Stage.ADDRESS_REVIEW.value if t.stage is Stage.PR_OPEN else t.stage.value


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


def operator_request(t: "TaskState") -> OperatorRequest | None:
    """What the operator is asked for, derived from the status: the page of
    a plan held or parked for review, or of an answers park; a waking task
    keeps its origin's until the launch."""
    return _request(t.status)


def _request(status: Status) -> OperatorRequest | None:
    if status.kind is Kind.WAKING:
        return _request(status.origin) if status.origin else None
    if status.kind in (Kind.HELD_PLAN_APPROVAL, Kind.PARKED_REVIEW):
        return PlanApprovalRequest(path=status.path, fingerprint=status.fingerprint)
    if status.kind is Kind.PARKED_ANSWERS:
        return AnswersRequest(path=status.path, fingerprint=status.fingerprint)
    return None

# A task that holds no E2E slot: one whose status does not occupy
# (status.occupies), for the save sets it whenever the status stops occupying.
NO_SLOT = -1

# No poke stretch (TaskState.poked_since / pokes): `replace(task, **NO_POKES)`
# at the clear points named on those fields.
NO_POKES = MappingProxyType({"poked_since": 0.0, "pokes": 0})


@dataclass(frozen=True)
class TaskState:
    issue: int
    repository: str
    stage: Stage
    slot: int
    worktree: str
    branch: str
    title: str
    updated_at: str  # read by main._grace_elapsed; touching this on a gate-parked task restarts its review clock
    # What the task does at its stage (dispatcher.status). The save refuses
    # one that breaks a constraint of design.md's Data model (_check).
    status: Status = RUNNING
    park_msg_id: int = 0                 # the Telegram message that asked; 0 = none
    hold_for_attach: bool = False
    effort: int | None = None            # board Effort at claim time
    labels: tuple[str, ...] = ()         # board labels at claim time
    track: str = ""                      # configured track name (spec/2026-09-14-model-tracks)
    base: str = "main"                   # branch the task was made from; fixed at claim, older files load as main
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
    # The plan revision the operator approved: the revision of a drained
    # `approve` intent (main._apply_answers_intent), the only proof a plan
    # `done` reads. A session writes its worktree, never this task file. A
    # field, not status detail: it outlives the statuses the approval passes
    # through (held, parked for review, waking, running). Cleared by end()
    # and by the transitions that stop it applying (main._kept_approval: a
    # new review round, the move off plan); "" = nothing approved.
    plan_approval: str = ""
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
    resume_text: str = ""                # what a resume wake's launch tells the session
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


def at_gate(t: TaskState) -> bool:
    """Does the plan session run at the review gate? Held there, or running
    at plan once it waited there (`gated`): reworking the plan after the
    operator's reply, or woken or respawned at the gate. Such a session's
    ready report is a review round the unattended limit counts, and its
    death respawns it. Whether its `done` is the approval is not read here:
    only the operator's recorded approval (`plan_approval`) says that."""
    return t.status.kind is Kind.HELD_PLAN_APPROVAL or (
        t.stage is Stage.PLAN and t.status.kind is Kind.RUNNING and t.gated)


# The doors: park, wake, unpark and end are the only writers of the status
# besides the claim and the transition executor (main._commit). Callers
# compose them with replace() for everything else (slot, updated_at, resume
# overrides).

def park(t: TaskState, status: Status, *, msg_id: int = 0) -> TaskState:
    """t parked (or held) in `status`, asked by message `msg_id`."""
    return replace(t, status=status, park_msg_id=msg_id, hold_for_attach=False)


def wake(t: TaskState, cause: Cause, *, hold: bool = False) -> TaskState:
    """t woken for `cause`: the wake is queued behind admission, and the
    status it wakes from stays as its origin until the launch; `hold` keeps
    the session for an attach. A task already waking stays as it is."""
    if t.status.kind is Kind.WAKING:
        return replace(t, hold_for_attach=hold)
    return replace(t, status=Status(Kind.WAKING, origin=t.status, cause=cause),
                   hold_for_attach=hold)


def unpark(t: TaskState) -> TaskState:
    """t's session runs."""
    return replace(t, status=RUNNING, park_msg_id=0, hold_for_attach=False)


def end(t: TaskState, status: Status) -> TaskState:
    """t ended in `status` (failed, canceled or done) at the stage it had."""
    return replace(t, status=status, park_msg_id=0, hold_for_attach=False,
                   plan_approval="")


def launch_track(t: TaskState, policy: ModelPolicy) -> str:
    """The track whose list t's next launch reads, or "" when
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


# The "waiting for a free slot" marker, wake-blocked-<repository>-<issue> in
# state_dir. Written by dispatcher.main, read by web.sources.
WAKE_BLOCKED_PREFIX = "wake-blocked-"


def task_key(repository: str, issue: int) -> str:
    """`<repository>-<issue>`: the (repository, issue) key every per-task file in
    state_dir is named by."""
    return f"{repository}-{issue}"


def parse_task_key(name: str) -> tuple[str, int] | None:
    """(repository, issue) from a `<repository>-<issue>` key, else None (including
    a legacy bare `<issue>`). rpartition, since repository names may themselves
    contain '-'."""
    repository, _, issue = name.rpartition("-")
    return (repository, int(issue)) if repository and issue.isdigit() else None


def _path(state_dir: str | Path, repository: str, issue: int) -> Path:
    return Path(state_dir) / f"task-{task_key(repository, issue)}.json"


def _legacy_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"task-{issue}.json"


def _previous(state_dir: str | Path, ts: TaskState) -> TaskState | None:
    """The state a save replaces; None also when the file cannot be read:
    the save is what repairs it."""
    try:
        return load(state_dir, ts.repository, ts.issue)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def save(state_dir: str | Path, ts: TaskState) -> None:
    """Write `ts`, refusing a status that breaks a constraint of design.md's
    Data model (_check). The slot follows the status (validated).
    terminal_at is stamped at the first ended status."""
    ts = validated(ts)
    ts = replace(ts, terminal_at=_terminal_at(_previous(state_dir, ts), ts))
    write_task(_path(state_dir, ts.repository, ts.issue), ts)
    # Lazy migration: the first save under the new key retires the legacy twin.
    _retire_legacy(state_dir, ts.repository, ts.issue)


def validated(ts: TaskState) -> TaskState:
    """`ts` as a save writes it, or ValueError for a status that breaks a
    constraint (_check). A status that does not occupy holds no slot."""
    _check(ts.stage, ts.status)
    if not occupies(ts.status):
        return replace(ts, slot=NO_SLOT)
    if ts.slot == NO_SLOT:
        raise ValueError(f"task {ts.repository}#{ts.issue}: {ts.status.kind.value} "
                         f"with no slot")
    return ts


def _terminal_at(previous: TaskState | None, ts: TaskState) -> str:
    if not ended(ts.status):
        return ""
    return (previous.terminal_at if previous and ended(previous.status)
            else ts.updated_at)


def _check(stage: Stage, status: Status) -> None:
    """Raise ValueError for a status the table cannot leave (statuses.problem)."""
    problem = statuses.problem(stage, status)
    if problem:
        raise ValueError(f"{problem} at {stage.value}")


def _retire_legacy(state_dir: str | Path, repository: str, issue: int) -> None:
    """Unlink task-{issue}.json only when it speaks for `repository`; another
    repository's task with the same issue number must survive. Read for its
    repository alone: the twin may be in the shape before the status."""
    p = _legacy_path(state_dir, issue)
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    if isinstance(d, dict) and d.get("repository", d.get("target")) == repository:
        p.unlink(missing_ok=True)


@contextmanager
def state_lock(state_dir: str | Path, name: str, timeout: float | None = None):
    """Hold an exclusive flock on state_dir/<name>. `timeout` None waits as
    long as it takes; else raise TimeoutError when the lock is still held
    after `timeout` seconds."""
    path = Path(state_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        _acquire(fh.fileno(), timeout, name)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _acquire(fd: int, timeout: float | None, name: str) -> None:
    if timeout is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{name} still held after {timeout}s") from None
            time.sleep(0.1)


def write_atomic(path: Path, data: str | bytes) -> None:
    """For a state file the web process and the dispatcher share (the usage
    cache, the priority mode): write a sibling temp file of its own and rename
    it over, so a reader never sees a torn one and two writers never share a
    temp file. mkstemp creates 0600 and the units may run as different users,
    so the file is opened up to 0644 before the rename. A failed write, chmod
    or rename leaves no temp file behind and the old content in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, doc: dict, *, indent: int | None = None,
                      sort_keys: bool = False) -> None:
    """write_atomic of json.dumps(doc, indent=indent, sort_keys=sort_keys)."""
    write_atomic(path, json.dumps(doc, indent=indent, sort_keys=sort_keys))


def write_task(p: Path, ts: TaskState) -> None:
    """Write `ts` to the task file `p` as it stands: no check, no key."""
    d = asdict(ts)
    d["stage"] = ts.stage.value
    d["status"] = statuses.to_json(ts.status)
    write_json_atomic(p, d, indent=2)


# The next launch of a task at one of these is a PR feedback round.
_PR_OPEN_STAGES = frozenset({Stage.PR_OPEN, Stage.ADDRESS_REVIEW})
_PAST_IMPLEMENT = _PR_OPEN_STAGES | {Stage.REVIEW}


def _migrate_implement_pick(d: dict) -> None:
    """State from before this change. No recorded provider: the implement
    pick's provider ran implement. Past implement the implement pick is no
    the implement session's any more: it becomes the feedback pick when the PR is open and
    there is none yet, so the next feedback round stays on its provider, and
    is dropped otherwise. Afterwards there is a provider and no such pick,
    so a second read (or a read after a save) changes nothing."""
    picks = d["picks"]
    if not d.get("implement_providers"):
        d["implement_providers"] = list(
            filter(None, [pick_provider(picks, Stage.IMPLEMENT.value)]))
    stage = d["stage"]
    if stage not in _PAST_IMPLEMENT or IMPLEMENT_PICK not in picks:
        return
    pick = picks.pop(IMPLEMENT_PICK)
    if stage in _PR_OPEN_STAGES:
        picks.setdefault(FEEDBACK_PICK, pick)


def _read_terminal(d: dict) -> None:
    if not ended(d["status"]):
        d["terminal_at"] = ""
        return
    d["terminal_at"] = d.get("terminal_at") or d.get("done_at") or d["updated_at"]


# Retired fields, dropped on load (ADR 0004): the park fields the status
# replaced (#132), the one implement session's ticket tracks, slice 24's
# artifact and the pending reply the message queue replaced.
_RETIRED = ("park", "park_reason", "park_note", "ci_run_id", "crashed_stage",
            "unlaunched", "operator_request", "feedback_pending",
            "ticket_cursor", "ticket_tracks", "ticket_names", "ticket_without_pick",
            "artifact", "pending_reply")


def _read(p: Path) -> TaskState | None:
    """The task stored at `p`; None when there is none. Raises on a stage or
    a status this format does not know: a retired stage is converted by the
    migration command, never here (ADR 0004)."""
    if not p.exists():
        return None
    return from_dict(json.loads(p.read_text()))


def from_dict(d: dict) -> TaskState:
    """The task a stored task file holds (_read); raises as _read does."""
    if "target" in d:   # files written before the rename
        d["repository"] = d.pop("target")
    d["stage"] = Stage(d["stage"])
    d["status"] = statuses.from_json(d["status"])
    _check(d["stage"], d["status"])
    _read_terminal(d)
    d["labels"] = tuple(d.get("labels", ()))
    d["picks"] = dict(d.get("picks") or {})
    _migrate_implement_pick(d)
    for retired in _RETIRED:
        d.pop(retired, None)
    return TaskState(**d)


def load(state_dir: str | Path, repository: str, issue: int) -> TaskState | None:
    ts = _read(_path(state_dir, repository, issue))
    if ts is not None:
        return ts
    legacy = _read(_legacy_path(state_dir, issue))
    # A legacy file speaks only for its own repository.
    return legacy if legacy is not None and legacy.repository == repository else None


def load_all(state_dir: str | Path) -> list[TaskState]:
    """Every task on disk that can be read (scan)."""
    return scan(state_dir)[0]


def scan(state_dir: str | Path) -> tuple[list[TaskState], list[Path]]:
    """Every task on disk, and the task files that cannot be read. A single
    corrupt file (truncated write, a shape this format does not know) is
    left out of the tasks rather than taking the whole scan down: callers
    like _prune_snapshots and capacity/slot accounting need every OTHER
    task's state to stay visible. It is named, so a claim can refuse to run
    while a task it cannot see may hold an issue and a slot. A
    directly-addressed file (load()) still raises."""
    tasks, unreadable = [], []
    for p in task_files(state_dir):
        try:
            ts = _read(p)
        except FileNotFoundError:
            continue   # deleted between the glob and the read
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            unreadable.append(p)
            continue
        # Identity comes from the JSON, never the filename stem.
        if ts is not None:
            tasks.append(ts)
    return sorted(tasks, key=lambda t: (t.repository, t.issue)), sorted(unreadable)


def task_files(state_dir: str | Path, *, archived: bool = False) -> list[Path]:
    """The top-level task files, a legacy `task-<issue>.json` twin among
    them, then the archived snapshots when `archived`. A snapshot repeats
    an ended task that may still have its own file, so a scan of the live
    tasks leaves them out."""
    root = Path(state_dir)
    files = sorted(root.glob("task-*.json"))
    return files + sorted(root.glob(f"{ARCHIVE_DIR}/*/{SNAPSHOT}")) if archived else files


def delete(state_dir: str | Path, repository: str, issue: int) -> None:
    _path(state_dir, repository, issue).unlink(missing_ok=True)
    _retire_legacy(state_dir, repository, issue)


def max_slots(capacity: int) -> int:
    """Slots are a resource NAMESPACE (ports 8100+slot / 5200+slot and the
    {slot} substitution in verify_cmd), not a concurrency limit — capacity is
    the concurrency limit. Two spare slots above capacity give a resume room
    to grab a fresh number while another session is mid-teardown, and deriving
    the ceiling means raising capacity: in targets.yaml can never silently
    reintroduce the starvation this replaced."""
    return capacity + 2


def allocate_slot(existing: list[TaskState], max_slots: int) -> int | None:
    held = {t.slot for t in existing if occupies(t.status)}
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
    d = read_stage_doc(worktree)
    return stage_signal(d) if d is not None else None


def read_stage_doc(worktree: str | Path) -> dict | None:
    """.agent/stage.json as a JSON object, None for anything else."""
    raw = read_regular(Path(worktree) / ".agent" / "stage.json", STAGE_SIGNAL_MAX_BYTES)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return d if isinstance(d, dict) else None


def stage_signal(d: dict) -> StageSignal | None:
    """The signal a stage.json object holds, None when it holds none."""
    try:
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


@dataclass(frozen=True)
class SessionRecord:
    """The conversation recorded for a task's continued stage."""

    session_id: str
    stage: str


def _session_path(state_dir: str | Path, repository: str, issue: int) -> Path:
    return Path(state_dir) / f"session-{task_key(repository, issue)}"


def write_session(state_dir: str | Path, repository: str, issue: int,
                  record: SessionRecord) -> None:
    """Replace a task's conversation atomically; only waitd writes it."""
    write_json_atomic(_session_path(state_dir, repository, issue), asdict(record))


def read_session(state_dir: str | Path, repository: str,
                 issue: int) -> SessionRecord | None:
    """Read a task's conversation; missing or malformed records are absent."""
    try:
        d = json.loads(_session_path(state_dir, repository, issue).read_text())
        session_id, stage = d["session_id"], d["stage"]
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(stage, str) or not stage:
            return None
        return SessionRecord(session_id, stage)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


def clear_session(state_dir: str | Path, repository: str, issue: int) -> None:
    """Remove only the task's conversation; a missing record is harmless."""
    _session_path(state_dir, repository, issue).unlink(missing_ok=True)


def _waiting_path(state_dir: str | Path, repository: str, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{task_key(repository, issue)}"


def _legacy_waiting_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{issue}"


def mark_waiting(state_dir: str | Path, repository: str, issue: int) -> None:
    p = _waiting_path(state_dir, repository, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    _background_path(state_dir, repository, issue).unlink(missing_ok=True)  # latest turn end wins


@dataclass(frozen=True)
class BackgroundWait:
    tasks: tuple[str, ...]   # identities the latest report named
    since: float             # cap clock: restarts when a report names new work
    reported: float          # time of the latest report


def _background_path(state_dir: str | Path, repository: str, issue: int) -> Path:
    return Path(state_dir) / f"background-{task_key(repository, issue)}"


def _identity(entry) -> str:
    if isinstance(entry, dict) and entry.get("id") is not None:
        return str(entry["id"])
    return json.dumps(entry, sort_keys=True)


def read_background(state_dir: str | Path, repository: str, issue: int) -> BackgroundWait | None:
    try:
        d = json.loads(_background_path(state_dir, repository, issue).read_text())
        return BackgroundWait(tuple(d["tasks"]), float(d["since"]), float(d["reported"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def mark_background(state_dir: str | Path, repository: str, issue: int,
                    tasks: list, now: float | None = None) -> None:
    now = time.time() if now is None else now
    ids = tuple(_identity(t) for t in tasks)
    old = read_background(state_dir, repository, issue)
    since = old.since if old and set(ids) <= set(old.tasks) else now
    p = _background_path(state_dir, repository, issue)
    _waiting_path(state_dir, repository, issue).unlink(missing_ok=True)  # latest turn end wins; before the write so a pass never sees both
    write_json_atomic(p, {"tasks": list(ids), "since": since, "reported": now})


def has_waiting(state_dir: str | Path, repository: str, issue: int) -> bool:
    return (_waiting_path(state_dir, repository, issue).exists()
            or _legacy_waiting_path(state_dir, issue).exists())


def clear_turn_markers(state_dir: str | Path, repository: str, issue: int) -> None:
    """Remove every turn-end marker (waiting, legacy waiting, background).
    The only way a background marker goes besides a waiting ping: every
    session end or replacement calls it, so a new session never inherits an
    old clock."""
    _waiting_path(state_dir, repository, issue).unlink(missing_ok=True)
    _legacy_waiting_path(state_dir, issue).unlink(missing_ok=True)
    _background_path(state_dir, repository, issue).unlink(missing_ok=True)


# An ended task's archive: artifacts/<key>/, its snapshot task.json in it.
ARCHIVE_DIR = "artifacts"
SNAPSHOT = "task.json"


def archive_root(state_dir: str | Path, repository: str, issue: int) -> Path:
    """Task archive namespace, shared by its snapshot and retained artifacts."""
    key = hashlib.sha256(f"{repository}\0{issue}".encode()).hexdigest()
    return Path(state_dir) / ARCHIVE_DIR / key


def archive(state_dir: str | Path, task: TaskState) -> None:
    if ended(task.status):
        write_task(archive_root(state_dir, task.repository, task.issue) / SNAPSHOT, task)


def load_archived(state_dir: str | Path, repository: str, issue: int) -> TaskState | None:
    task = _read(archive_root(state_dir, repository, issue) / SNAPSHOT)
    return task if task and ended(task.status) else None
