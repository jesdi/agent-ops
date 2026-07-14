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


@dataclass(frozen=True)
class StageSignal:
    stage: str
    status: str  # working | awaiting-review | done | blocked
    note: str = ""
    artifact: str = ""


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
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
