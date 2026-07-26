"""Pure view models: TaskState + rank rows + budget + failures + events ->
Pydantic responses. NO I/O in this module — construction only."""
from __future__ import annotations

from pydantic import BaseModel

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
