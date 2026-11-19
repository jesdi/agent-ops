"""park / wake / unpark / end: the writers of a task's status besides the
claim and the transition executor (#132)."""
import json

from dispatcher.state import Stage, TaskState, end, load, park, save, unpark, wake
from dispatcher.status import Cause, Kind
from tests.statuses import (DONE, RUNNING, answers, ci, failed, human, login, review,
                            waking)

ASKED = dict(status=human("decision", "which DB?"), park_msg_id=7, hold_for_attach=True)


def make(stage=Stage.IMPLEMENT, slot=1, **kw) -> TaskState:
    return TaskState(issue=42, repository="portfolio_eval", stage=stage, slot=slot,
                     worktree="/wt", branch="agent/task-42", title="t",
                     updated_at="2026-11-13T00:00:00+00:00", **kw)


def test_park_sets_the_status_and_its_message_and_drops_an_attach_hold():
    t = park(make(**ASKED), review("review.html", "plan ready"), msg_id=9)
    assert (t.status, t.park_msg_id, t.hold_for_attach) == (
        review("review.html", "plan ready"), 9, False)


def test_park_leaves_every_other_field_alone():
    t = make(slot=3, resume_model_override="anthropic/opus", asked=True, plan_approval="ab")
    assert park(t, login("login")).slot == 3
    parked = park(t, human("decision", "q"), msg_id=1)
    assert (parked.resume_model_override, parked.asked, parked.stage, parked.plan_approval) == (
        "anthropic/opus", True, Stage.IMPLEMENT, "ab")


def test_a_wake_keeps_the_status_it_comes_from_as_its_origin():
    t = wake(make(**ASKED), Cause.REPLY)
    assert t.status == waking(human("decision", "which DB?"), "reply")
    assert t.park_msg_id == 7


def test_a_wake_for_attach_holds_the_session():
    assert wake(make(status=human()), Cause.REPLY, hold=True).hold_for_attach


def test_a_wake_of_a_waking_task_keeps_its_origin_and_cause():
    t = wake(make(status=waking(failed(), "resume")), Cause.REPLY, hold=True)
    assert (t.status, t.hold_for_attach) == (waking(failed(), "resume"), True)


def test_unpark_runs_the_session_and_drops_the_question():
    t = unpark(make(**ASKED))
    assert (t.status, t.park_msg_id, t.hold_for_attach) == (RUNNING, 0, False)


def test_end_keeps_the_stage_and_drops_the_question_and_the_approval():
    t = end(make(**ASKED, plan_approval="ab"), failed("killed"))
    assert (t.stage, t.status, t.park_msg_id, t.hold_for_attach, t.plan_approval) == (
        Stage.IMPLEMENT, failed("killed"), 0, False, "")


def test_doors_change_no_key_of_the_task_file(tmp_path):
    save(tmp_path, make())
    keys = set(json.loads((tmp_path / "task-portfolio_eval-42.json").read_text()))
    for t in (park(make(), human("decision", "q")), wake(make(**ASKED), Cause.REPLY),
              unpark(make(**ASKED)), end(make(**ASKED), DONE),
              park(make(stage=Stage.SPEC), answers("q.html")), park(make(), ci(9))):
        save(tmp_path, t)
        assert set(json.loads(
            (tmp_path / "task-portfolio_eval-42.json").read_text())) == keys


def test_a_failed_stage_signal_ends_the_task_failed_at_its_stage(tmp_path, monkeypatch):
    """awaiting-ci with no run id fails the task with an artifact failure,
    at the stage it had; it keeps no question of an earlier park."""
    from datetime import datetime, timezone

    import dispatcher.main as main
    from tests.dispatchfakes import FakeSessions, cfg, deps, make_task, patch_usage

    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, updated_at=datetime.now(timezone.utc).isoformat(), park_msg_id=7)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status, t.park_msg_id) == (Stage.IMPLEMENT, failed("artifact"), 0)
    assert t.status.kind is Kind.FAILED
