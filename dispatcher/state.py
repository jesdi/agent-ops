"""Per-task runtime state (~/agent-ops-state/task-<issue>.json) and the
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
    BLOCKED = "blocked"
    FAILED = "failed"
    STALLED_ON_BUDGET = "stalled-on-budget"


# Stages that occupy capacity and an E2E slot. BLOCKED and
# STALLED_ON_BUDGET still hold a live session/worktree, so they count.
IN_FLIGHT_STAGES = frozenset({
    Stage.QUEUED,
    Stage.SPEC,
    Stage.AWAITING_SPEC_REVIEW,
    Stage.PLAN,
    Stage.IMPLEMENT,
    Stage.BLOCKED,
    Stage.STALLED_ON_BUDGET,
})

MAX_SLOTS = 3

# Park lifecycle (orthogonal to stage — the stage is preserved while parked).
# "" = not parked. Parked tasks release CAPACITY but keep their SLOT
# (ports/worktree stay reserved until the task truly ends).
PARK_HUMAN = "parked"            # waiting for operator input
PARK_CI = "awaiting-ci"          # waiting for a GitHub Actions run
PARK_WAKE = "unpark-requested"   # wake event arrived; resume when slot free
PARK_LOGIN = "parked-login"      # live session sitting at a /login prompt


@dataclass(frozen=True)
class TaskState:
    issue: int
    target: str
    stage: Stage
    slot: int
    worktree: str
    branch: str
    title: str
    updated_at: str
    park: str = ""
    ci_run_id: int = 0
    park_msg_id: int = 0
    pending_reply: str = ""
    hold_for_attach: bool = False
    effort: int | None = None            # board Effort at claim time
    labels: tuple[str, ...] = ()         # board labels at claim time
    plan_retries: int = 0                # in-session plan-format retries used
    artifact: str = ""                   # spec path while at the review gate


@dataclass(frozen=True)
class StageSignal:
    stage: str
    status: str  # working | awaiting-review | done | blocked | awaiting-ci
    note: str = ""
    artifact: str = ""
    run_id: int = 0


def _path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"task-{issue}.json"


def save(state_dir: str | Path, ts: TaskState) -> None:
    d = asdict(ts)
    d["stage"] = ts.stage.value
    p = _path(state_dir, ts.issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


def load(state_dir: str | Path, issue: int) -> TaskState | None:
    p = _path(state_dir, issue)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    d["stage"] = Stage(d["stage"])
    d["labels"] = tuple(d.get("labels", ()))
    return TaskState(**d)


def load_all(state_dir: str | Path) -> list[TaskState]:
    root = Path(state_dir)
    if not root.exists():
        return []
    tasks = []
    for p in root.glob("task-*.json"):
        issue = int(p.stem.removeprefix("task-"))
        ts = load(root, issue)
        if ts is not None:
            tasks.append(ts)
    return sorted(tasks, key=lambda t: t.issue)


def allocate_slot(existing: list[TaskState]) -> int | None:
    held = {t.slot for t in existing if t.stage in IN_FLIGHT_STAGES}
    for slot in range(MAX_SLOTS):
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


def active(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if t.stage in IN_FLIGHT_STAGES and not t.park]


def parked(tasks: list[TaskState]) -> list[TaskState]:
    return [t for t in tasks if t.stage in IN_FLIGHT_STAGES and t.park]


def _waiting_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"waiting-{issue}"


def mark_waiting(state_dir: str | Path, issue: int) -> None:
    p = _waiting_path(state_dir, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def has_waiting(state_dir: str | Path, issue: int) -> bool:
    return _waiting_path(state_dir, issue).exists()


def clear_waiting(state_dir: str | Path, issue: int) -> None:
    _waiting_path(state_dir, issue).unlink(missing_ok=True)


def _attached_path(state_dir: str | Path, issue: int) -> Path:
    return Path(state_dir) / f"attached-{issue}"


def mark_attached(state_dir: str | Path, issue: int) -> None:
    """A human is attached to this task's tmux (web terminal, Plan 2).
    While the marker exists the dispatcher holds the task: no resume, no
    park, no reap — the meaning hold_for_attach already carries."""
    p = _attached_path(state_dir, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def has_attached(state_dir: str | Path, issue: int) -> bool:
    return _attached_path(state_dir, issue).exists()


def clear_attached(state_dir: str | Path, issue: int) -> None:
    _attached_path(state_dir, issue).unlink(missing_ok=True)
