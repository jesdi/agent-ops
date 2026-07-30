"""Pure view-model tests: every (stage, park) combination, board grouping."""
import pytest

from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_LOGIN,
                              PARK_REVIEW, PARK_WAKE, Stage)
from tests.webfakes import make_task
from web.read_model import build_board, column_for, task_card

STAGE_COLUMNS = {
    Stage.QUEUED: "queued",
    Stage.SPEC: "in-progress",
    Stage.PLAN: "in-progress",
    Stage.IMPLEMENT: "in-progress",
    Stage.AWAITING_SPEC_REVIEW: "needs-review",
    Stage.PR_OPEN: "pr-open",
    Stage.FAILED: "failed",
    Stage.BLOCKED: "failed",         # legacy stage: surfaced, not hidden
    Stage.STALLED_ON_BUDGET: "stalled",
}


@pytest.mark.parametrize("stage", list(Stage))
def test_park_overrides_stage(stage):
    assert column_for(stage.value, PARK_HUMAN) == "parked"
    assert column_for(stage.value, PARK_CI) == "awaiting-ci"
    assert column_for(stage.value, PARK_WAKE) == "resuming"
    # a login park is the state that most needs an operator — never let it
    # render as healthy in-progress work
    assert column_for(stage.value, PARK_LOGIN) == "parked"
    assert column_for(stage.value, PARK_REVIEW) == "needs-review"


@pytest.mark.parametrize("stage", list(Stage))
def test_unparked_column_per_stage(stage):
    assert column_for(stage.value, "") == STAGE_COLUMNS[stage]


def test_task_card_fields():
    t = make_task(issue=7, stage=Stage.SPEC, slot=1, park=PARK_HUMAN,
                  park_msg_id=0)
    card = task_card(t, model="claude-sonnet-4-5", attached=True)
    assert card.issue == 7
    assert card.stage == "spec"
    assert card.park == "parked"
    assert card.column == "parked"
    assert card.model == "claude-sonnet-4-5"
    assert card.attached is True
    # parked-for-human with no Telegram message id yet -> note pending
    assert card.park_note_pending is True


def test_park_note_not_pending_once_notified():
    t = make_task(park=PARK_HUMAN, park_msg_id=123)
    assert task_card(t, model="m", attached=False).park_note_pending is False


def test_login_park_card_shows_its_park_kind_in_the_parked_column():
    t = make_task(issue=9, stage=Stage.IMPLEMENT, park=PARK_LOGIN,
                  park_msg_id=77)
    card = task_card(t, model="m", attached=False)
    assert card.column == "parked"
    assert card.park == PARK_LOGIN  # the card still distinguishes the kind
    # a login park always has a Telegram message to reply to (a failed send
    # degrades to a plain park), so it is never a "note pending" card
    assert card.park_note_pending is False


def test_login_park_counts_towards_active_capacity():
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT, slot=0, park=PARK_LOGIN),
             make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN)]
    board = build_board(tasks, capacity=2, models={}, attached=set())
    assert board.capacity.active == 1   # matches dispatcher.state.active()
    assert board.capacity.slots_used == 2


def test_build_board_groups_and_counts():
    tasks = [
        make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
        make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN),
        make_task(issue=3, stage=Stage.PR_OPEN, slot=0),
    ]
    board = build_board(tasks, capacity=2,
                        models={1: "a", 2: "b", 3: "c"}, attached={1})
    by_key = {c.key: c for c in board.columns}
    assert [c.issue for c in by_key["in-progress"].cards] == [1]
    assert [c.issue for c in by_key["parked"].cards] == [2]
    assert [c.issue for c in by_key["pr-open"].cards] == [3]
    assert by_key["in-progress"].cards[0].attached is True
    # capacity: parked releases capacity but keeps its slot;
    # pr-open is not in-flight at all.
    assert board.capacity.active == 1
    assert board.capacity.slots_used == 2
    assert board.capacity.capacity == 2
    assert board.capacity.max_slots == 3


def test_board_column_order_is_stable():
    board = build_board([], capacity=2, models={}, attached=set())
    assert [c.key for c in board.columns] == [
        "queued", "in-progress", "needs-review", "pr-open", "parked",
        "awaiting-ci", "resuming", "stalled", "failed"]


def test_review_park_shows_in_the_needs_review_column():
    t = make_task(issue=9, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
                  park=PARK_REVIEW, park_msg_id=77)
    card = task_card(t, model="m", attached=False)
    assert card.column == "needs-review"
    assert card.park == PARK_REVIEW   # the card still distinguishes the kind
    assert card.park_note_pending is False


def test_gate_parked_tasks_hold_neither_capacity_nor_a_slot():
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
             make_task(issue=2, stage=Stage.AWAITING_SPEC_REVIEW,
                       slot=NO_SLOT, park=PARK_REVIEW)]
    board = build_board(tasks, capacity=2, models={}, attached=set())
    assert board.capacity.active == 1
    assert board.capacity.slots_used == 1   # not 2 — #2 gave its slot back


def test_task_card_carries_park_note():
    t = make_task(park="parked", park_note="which URL?")
    card = task_card(t, model="opus", attached=False)
    assert card.park_note == "which URL?"
