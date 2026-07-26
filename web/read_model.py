"""Pure view models: TaskState + rank rows + budget + failures + events ->
Pydantic responses. NO I/O in this module — construction only."""
from __future__ import annotations

from pydantic import BaseModel

from dispatcher.budget import UsageSnapshot, should_spawn
from dispatcher.state import (IN_FLIGHT_STAGES, MAX_SLOTS, PARK_CI,
                              PARK_HUMAN, PARK_WAKE, Stage, TaskState)

# (key, title) in display order — the single place column semantics live.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("queued", "Queued"),
    ("in-progress", "In progress"),
    ("needs-review", "Needs review"),
    ("pr-open", "PR open"),
    ("parked", "Parked"),
    ("awaiting-ci", "Awaiting CI"),
    ("resuming", "Resuming"),
    ("stalled", "Stalled on budget"),
    ("failed", "Failed"),
)

_PARK_COLUMN = {PARK_HUMAN: "parked", PARK_CI: "awaiting-ci",
                PARK_WAKE: "resuming"}
_STAGE_COLUMN = {
    Stage.QUEUED.value: "queued",
    Stage.SPEC.value: "in-progress",
    Stage.PLAN.value: "in-progress",
    Stage.IMPLEMENT.value: "in-progress",
    Stage.AWAITING_SPEC_REVIEW.value: "needs-review",
    Stage.PR_OPEN.value: "pr-open",
    Stage.FAILED.value: "failed",
    Stage.BLOCKED.value: "failed",  # legacy stage, surfaced not hidden
    Stage.STALLED_ON_BUDGET.value: "stalled",
}


def column_for(stage: str, park: str) -> str:
    if park in _PARK_COLUMN:
        return _PARK_COLUMN[park]
    return _STAGE_COLUMN[stage]


class TaskCard(BaseModel):
    issue: int
    target: str
    title: str
    stage: str
    park: str
    column: str
    slot: int
    branch: str
    model: str
    park_note_pending: bool
    updated_at: str
    attached: bool


class Column(BaseModel):
    key: str
    title: str
    cards: list[TaskCard]


class CapacityView(BaseModel):
    active: int
    capacity: int
    slots_used: int
    max_slots: int


class BoardView(BaseModel):
    columns: list[Column]
    capacity: CapacityView


def task_card(t: TaskState, *, model: str, attached: bool) -> TaskCard:
    return TaskCard(
        issue=t.issue, target=t.target, title=t.title,
        stage=t.stage.value, park=t.park,
        column=column_for(t.stage.value, t.park),
        slot=t.slot, branch=t.branch, model=model,
        park_note_pending=(t.park == PARK_HUMAN and t.park_msg_id == 0),
        updated_at=t.updated_at, attached=attached)


def build_board(tasks: list[TaskState], *, capacity: int,
                models: dict[int, str], attached: set[int]) -> BoardView:
    cards = [task_card(t, model=models.get(t.issue, ""),
                       attached=t.issue in attached) for t in tasks]
    by_column: dict[str, list[TaskCard]] = {key: [] for key, _ in COLUMNS}
    for card in cards:
        by_column[card.column].append(card)
    in_flight = [t for t in tasks if t.stage in IN_FLIGHT_STAGES]
    return BoardView(
        columns=[Column(key=key, title=title, cards=by_column[key])
                 for key, title in COLUMNS],
        capacity=CapacityView(
            active=len([t for t in in_flight if not t.park]),
            capacity=capacity,
            slots_used=len(in_flight),
            max_slots=MAX_SLOTS))


class TaskDetail(BaseModel):
    card: TaskCard
    pane_tail: str
    session_alive: bool
    worktree: str
    pending_reply: str
    ci_run_id: int
    effort: int | None
    labels: list[str]


def task_detail(t: TaskState, *, model: str, attached: bool,
                pane_tail: str, session_alive: bool) -> TaskDetail:
    return TaskDetail(
        card=task_card(t, model=model, attached=attached),
        pane_tail=pane_tail, session_alive=session_alive,
        worktree=t.worktree, pending_reply=t.pending_reply,
        ci_run_id=t.ci_run_id, effort=t.effort, labels=list(t.labels))


class BudgetView(BaseModel):
    utilization: float
    minutes_to_reset: float
    source: str
    would_spawn: bool
    threshold_applied: str


def budget_view(u: UsageSnapshot, threshold: float, racing_minutes: int,
                racing_threshold: float) -> BudgetView:
    if u.source == "unavailable":
        applied = "n/a"
    elif u.minutes_to_reset <= racing_minutes:
        applied = "reset-racing"
    else:
        applied = "base"
    return BudgetView(
        utilization=u.utilization, minutes_to_reset=u.minutes_to_reset,
        source=u.source,
        would_spawn=should_spawn(u, threshold, racing_minutes,
                                 racing_threshold),
        threshold_applied=applied)


class QuarantineEntry(BaseModel):
    target: str
    task_issue: int
    blocker_repo: str
    blocker_issue: int
    fingerprint: str
    created_at: str
    blocker_open: bool | None


class FingerprintEntry(BaseModel):
    fingerprint: str
    repo: str
    issue: int
    when: str


class FailuresView(BaseModel):
    quarantined: list[QuarantineEntry]
    fingerprints: list[FingerprintEntry]


class EventEntry(BaseModel):
    ts: str
    event: str
    target: str
    issue: int
    stage: str
    model: str
    actor: str
    detail: str


class HistoryView(BaseModel):
    events: list[EventEntry]
