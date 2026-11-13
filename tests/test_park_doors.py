"""park / unpark / end: the only writers of a task's park fields (#132)."""
import json

import pytest

from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PARK_WAKE, AnswersRequest, PlanApprovalRequest,
                              Stage, TaskState, end, load, park, save, unpark)

PARK_FIELDS = ("park", "park_reason", "park_note", "park_msg_id", "ci_run_id",
               "crashed_stage", "hold_for_attach", "operator_request")
STALE = dict(park=PARK_HUMAN, park_reason="decision", park_note="which DB?",
             park_msg_id=7, ci_run_id=991, hold_for_attach=True,
             operator_request=AnswersRequest(path="q.html", fingerprint="ab12"))


def make(stage=Stage.IMPLEMENT, slot=1, **kw) -> TaskState:
    return TaskState(issue=42, repository="portfolio_eval", stage=stage, slot=slot,
                     worktree="/wt", branch="agent/task-42", title="t",
                     updated_at="2026-11-13T00:00:00+00:00", **kw)


def fields(t: TaskState) -> dict:
    return {f: getattr(t, f) for f in PARK_FIELDS}


def test_park_sets_every_park_field_and_drops_what_an_earlier_park_left():
    req = PlanApprovalRequest(path="review.html", fingerprint="cd34")
    t = park(make(**STALE), PARK_REVIEW, note="plan ready", msg_id=9, request=req)
    assert fields(t) == dict(park=PARK_REVIEW, park_reason="", park_note="plan ready",
                             park_msg_id=9, ci_run_id=0, crashed_stage="",
                             hold_for_attach=False, operator_request=req)


def test_park_for_ci_records_the_run_and_nothing_else():
    t = park(make(**STALE), PARK_CI, ci_run_id=991)
    assert fields(t) == dict(park=PARK_CI, park_reason="", park_note="",
                             park_msg_id=0, ci_run_id=991, crashed_stage="",
                             hold_for_attach=False, operator_request=None)


def test_park_leaves_every_other_field_alone():
    t = make(slot=3, resume_model_override="anthropic/opus", asked=True)
    assert park(t, PARK_LOGIN, note="login").slot == 3
    parked = park(t, PARK_HUMAN, note="q", reason="decision", msg_id=1)
    assert (parked.resume_model_override, parked.asked, parked.stage) == (
        "anthropic/opus", True, Stage.IMPLEMENT)


def test_a_wake_keeps_the_park_it_comes_from_and_drops_the_ci_run():
    t = unpark(make(**STALE), waking=True)
    assert fields(t) == {**fields(make(**STALE)), "park": PARK_WAKE,
                         "ci_run_id": 0, "hold_for_attach": False}


def test_a_wake_for_attach_holds_the_session():
    assert unpark(make(park=PARK_HUMAN), waking=True, hold=True).hold_for_attach


def test_a_wake_of_a_crashed_task_keeps_its_crashed_stage():
    t = unpark(make(crashed_stage="implement"), waking=True)
    assert (t.park, t.crashed_stage) == (PARK_WAKE, "implement")


def test_unpark_to_running_clears_every_park_field():
    t = unpark(make(**STALE, crashed_stage="implement"))
    assert fields(t) == fields(make())


@pytest.mark.parametrize("stage", [Stage.DONE, Stage.FAILED, Stage.CANCELED])
def test_end_is_never_parked_and_leaves_no_park_field_behind(stage):
    t = end(make(**STALE, crashed_stage="implement"), stage)
    assert t.stage is stage
    assert fields(t) == fields(make())


def test_a_crash_ends_failed_and_remembers_its_stage():
    t = end(make(**STALE), Stage.FAILED, crashed_stage="implement")
    assert (t.stage, t.crashed_stage, t.park, t.park_note) == (
        Stage.FAILED, "implement", "", "")


def test_a_terminal_file_loads_through_end_and_keeps_its_crashed_stage(tmp_path):
    save(tmp_path, make(stage=Stage.FAILED, **STALE, crashed_stage="implement"))
    t = load(tmp_path, "portfolio_eval", 42)
    assert fields(t) == {**fields(make()), "crashed_stage": "implement"}


def test_doors_change_no_key_of_the_task_file(tmp_path):
    save(tmp_path, make())
    keys = set(json.loads((tmp_path / "task-portfolio_eval-42.json").read_text()))
    for t in (park(make(), PARK_HUMAN, note="q"), unpark(make(**STALE), waking=True),
              unpark(make(**STALE)), end(make(**STALE), Stage.DONE)):
        save(tmp_path, t)
        assert set(json.loads(
            (tmp_path / "task-portfolio_eval-42.json").read_text())) == keys
