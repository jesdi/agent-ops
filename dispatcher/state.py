"""Per-task runtime state (~/agent-ops-state/task-<target>-<issue>.json) and the
stage.json signal sessions write into their worktree."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


class Stage(str, Enum):
    QUEUED = "queued"
    SPEC = "spec"
    AWAITING_SPEC_REVIEW = "awaiting-spec-review"
    PLAN = "plan"
    IMPLEMENT = "implement"
    PR_OPEN = "pr-open"
    ADDRESS_REVIEW = "address-review"
    BLOCKED = "blocked"
    FAILED = "failed"
    STALLED_ON_BUDGET = "stalled-on-budget"
    DONE = "done"
    CANCELED = "canceled"  # operator won't-do; terminal like FAILED/DONE


# Stages that occupy capacity and an E2E slot. BLOCKED and
# STALLED_ON_BUDGET still hold a live session/worktree, so they count.
IN_FLIGHT_STAGES = frozenset({
    Stage.QUEUED,
    Stage.SPEC,
    Stage.AWAITING_SPEC_REVIEW,
    Stage.PLAN,
    Stage.IMPLEMENT,
    Stage.ADDRESS_REVIEW,
    Stage.BLOCKED,
    Stage.STALLED_ON_BUDGET,
})

# A task that holds no E2E slot. Every session-ending park releases its slot
# back to the pool; only PARK_LOGIN keeps a slot because it keeps a live
# container and tmux session running (the pane is where the operator types the
# OAuth code). PARK_REVIEW is the original exception that freed the slot; the
# rule now applies to all session-ending parks, so slots are never leaked.
NO_SLOT = -1

# Park lifecycle (orthogonal to stage — the stage is preserved while parked).
# "" = not parked. Parked tasks release CAPACITY. Every park that ends the
# session also releases its SLOT — the only exception is PARK_LOGIN, which
# keeps a live container and tmux session and therefore keeps both capacity
# and the slot (see active() and holds_slot()).
PARK_HUMAN = "parked"            # waiting for operator input
PARK_CI = "awaiting-ci"          # waiting for a GitHub Actions run
PARK_WAKE = "unpark-requested"   # wake event arrived; resume when slot free
PARK_LOGIN = "parked-login"      # live session sitting at a /login prompt
PARK_REVIEW = "awaiting-review"  # spec done, parked for review at leisure


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
    plan_retries: int = 0                # in-session plan-format retries used
    artifact: str = ""                   # spec path while at the review gate
    pr_number: int = 0                   # the task's PR; 0 = not yet resolved
    feedback_cursor: str = ""            # ISO ts; "" = any human feedback is new
    feedback_pending: bool = False       # feedback seen, address-review deferred
    done_at: str = ""                    # merge-detection time; drives the flush


@dataclass(frozen=True)
class StageSignal:
    stage: str
    status: str  # working | awaiting-review | done | blocked | awaiting-ci
    note: str = ""
    artifact: str = ""
    run_id: int = 0


def _path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"task-{target}-{issue}.json"


def _legacy_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"task-{issue}.json"


def save(state_dir: str | Path, ts: TaskState) -> None:
    d = asdict(ts)
    d["stage"] = ts.stage.value
    p = _path(state_dir, ts.target, ts.issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)
    # Lazy migration: the first save under the new key retires the legacy twin.
    _legacy_path(state_dir, ts.issue).unlink(missing_ok=True)


def _read(p: Path) -> TaskState | None:
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    d["stage"] = Stage(d["stage"])
    d["labels"] = tuple(d.get("labels", ()))
    d.pop("pending_reply", None)   # retired field, see original comment
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
    legacy = _read(_legacy_path(state_dir, issue))
    if legacy is not None and legacy.target == target:
        _legacy_path(state_dir, issue).unlink(missing_ok=True)


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


def read_stage_signal(worktree: str | Path) -> StageSignal | None:
    p = Path(worktree) / ".agent" / "stage.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return StageSignal(
            stage=str(d["stage"]),
            status=str(d["status"]),
            note=str(d.get("note", "")),
            artifact=str(d.get("artifact", "")),
            run_id=int(d.get("run_id", 0) or 0),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
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
    return Path(state_dir) / f"waiting-{target}-{issue}"


def _legacy_waiting_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{issue}"


def mark_waiting(state_dir: str | Path, target: str, issue: int) -> None:
    p = _waiting_path(state_dir, target, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def has_waiting(state_dir: str | Path, target: str, issue: int) -> bool:
    return (_waiting_path(state_dir, target, issue).exists()
            or _legacy_waiting_path(state_dir, issue).exists())


def clear_waiting(state_dir: str | Path, target: str, issue: int) -> None:
    _waiting_path(state_dir, target, issue).unlink(missing_ok=True)
    _legacy_waiting_path(state_dir, issue).unlink(missing_ok=True)


def _attached_path(state_dir: str | Path, target: str, issue: int) -> Path:
    return Path(state_dir) / f"attached-{target}-{issue}"


def _legacy_attached_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"attached-{issue}"


def mark_attached(state_dir: str | Path, target: str, issue: int) -> None:
    """A human is attached to this task's tmux (web terminal, Plan 2).
    While the marker exists the dispatcher holds the task: no resume, no
    park, no reap — the meaning hold_for_attach already carries."""
    p = _attached_path(state_dir, target, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def has_attached(state_dir: str | Path, target: str, issue: int) -> bool:
    return (_attached_path(state_dir, target, issue).exists()
            or _legacy_attached_path(state_dir, issue).exists())


def clear_attached(state_dir: str | Path, target: str, issue: int) -> None:
    _attached_path(state_dir, target, issue).unlink(missing_ok=True)
    _legacy_attached_path(state_dir, issue).unlink(missing_ok=True)
