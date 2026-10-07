"""Nothing changes an accepted ticket set, and what follows from that: the
legacy mark goes when its ticket is done; a denied override is no reason to
park; a turn that fails after it saved a stage with no session stays
resumable; a task from before tickets; the label and the models.log line
name the launch."""
import json
from dataclasses import replace

import dispatcher.main as main
from dispatcher import execution_overrides, state
from dispatcher.machine import Notify, SpawnStage, StartTicket, next_actions
from dispatcher.state import (PARK_HUMAN, Stage, StageSignal, after_ticket,
                              next_launch, resumable_crash, save,
                              shown_stage)
from tests import webfakes
from tests.pinned import (ASTRA, DENY_ARCHITECTURE, DENY_FRONTEND, FABLE,
                          IMPL_DONE, OPUS, PLAN_DONE, SOL, ahead, anthropic,
                          cards, deny, launched, legacy, make_cfg, openai,
                          raw, rig, saved, setup, step, task, unpinned,
                          usage_now, wake, write_ticket)
from tests.test_main import (FakeNotifier, FakeSessions, deps, make_task,
                             valid_spec)


def log_lines(wt):
    p = wt / ".agent" / "models.log"
    return [" ".join(line.split()[1:]) for line in p.read_text().splitlines()]


# --- 1: the legacy mark goes when the legacy ticket is done ------------------

def test_a_done_legacy_ticket_is_no_longer_in_progress(tmp_path, monkeypatch):
    c, wt = legacy(tmp_path, monkeypatch)            # ticket 2 of 3, no pick
    assert saved(c).ticket_without_pick
    usage_now(monkeypatch, deny(ahead(), *DENY_ARCHITECTURE))
    sess = step(c, wt, IMPL_DONE)
    assert sess.spawned == []
    assert raw(c)["ticket_without_pick"] is False
    assert next_launch(saved(c)) == ("implement", 3)
    assert f"implement [{ASTRA}@medium]" in main._status_lines(c)[0]
    usage_now(monkeypatch, ahead())
    sess = wake(c)
    assert sess.resumed == [] and "03-t3.md" in sess.spawned[0][3]


def test_a_wake_after_a_legacy_tasks_last_ticket_reaches_review(
        tmp_path, monkeypatch):
    c, wt = legacy(tmp_path, monkeypatch)
    save(c.state_dir, replace(saved(c), ticket_cursor=3))
    usage_now(monkeypatch, deny(ahead(), *DENY_ARCHITECTURE))
    assert step(c, wt, IMPL_DONE).spawned == []
    assert raw(c)["ticket_without_pick"] is False
    usage_now(monkeypatch, ahead())
    sess = wake(c)
    assert sess.resumed == [] and [s[1] for s in sess.spawned] == ["review"]


# --- 2: a stored override that the gate denies is not a reason to park --------

def test_a_denied_override_of_an_unpinned_ticket_waits_and_stays_stored(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=SOL, bypass_usage=False))
    c2 = unpinned(c)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    assert step(c2, wt, IMPL_DONE).spawned == []
    assert not saved(c).park
    stored = execution_overrides.load(c.state_dir, "portfolio_eval", 42)
    assert stored is not None and stored.model == SOL
    usage_now(monkeypatch, ahead())
    assert launched(step(c2, wt, IMPL_DONE)) == [(SOL, "")]
    assert saved(c).ticket_cursor == 2


# --- 3: nothing changes an accepted ticket set ---------------------------------

def accepted(tmp_path, monkeypatch, admitted=False):
    usages = ahead() if admitted else deny(ahead(), *DENY_ARCHITECTURE)
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()], usages=usages)
    return c, wt, step(c, wt, PLAN_DONE)


def test_the_plan_session_is_ended_when_the_set_is_accepted(
        tmp_path, monkeypatch):
    c, wt, sess = accepted(tmp_path, monkeypatch)
    assert sess.spawned == [] and sess.ended == [42]
    assert saved(c).stage is Stage.IMPLEMENT


def test_an_accepted_task_with_no_session_waits_without_a_write(
        tmp_path, monkeypatch):
    c, wt, _ = accepted(tmp_path, monkeypatch)
    p = state._path(c.state_dir, "portfolio_eval", 42)
    before = p.read_text()
    for _ in range(2):
        sess = FakeSessions()                      # the plan session is gone
        main.run_pass(c, deps(sess=sess))
        assert sess.spawned == [] and sess.resumed == [] and sess.ended == []
    assert p.read_text() == before
    t = saved(c)
    assert (t.stage, t.park, t.slot) == (Stage.IMPLEMENT, "", 0)
    assert "capacity 1/" in main._status_lines(c)[-1]


def test_ticket_1_does_not_end_the_plan_session_twice(tmp_path, monkeypatch):
    c, wt, sess = accepted(tmp_path, monkeypatch, admitted=True)
    assert len(sess.spawned) == 1 and sess.ended == [42]


def test_a_later_start_of_ticket_1_ends_no_session(tmp_path, monkeypatch):
    c, wt, _ = accepted(tmp_path, monkeypatch)
    usage_now(monkeypatch, ahead())
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert len(sess.spawned) == 1 and sess.ended == []


def test_a_ticket_set_grown_after_acceptance_parks_before_review(
        tmp_path, monkeypatch):
    c, wt, _ = accepted(tmp_path, monkeypatch, admitted=True)
    step(c, wt, IMPL_DONE)                          # ticket 2 runs
    write_ticket(wt, 3)
    sess = step(c, wt, IMPL_DONE)
    assert sess.spawned == []
    t = saved(c)
    assert t.park == PARK_HUMAN and t.stage is Stage.IMPLEMENT
    assert "2 ticket" in t.park_note and "03-t3.md" in t.park_note


def test_a_ticket_file_removed_after_acceptance_still_fails_the_task(
        tmp_path, monkeypatch):
    c, wt, _ = accepted(tmp_path, monkeypatch, admitted=True)
    (wt / ".agent" / "tickets" / "02-t2.md").unlink()
    step(c, wt, IMPL_DONE)
    t = saved(c)
    assert t.stage is Stage.FAILED and t.ticket_cursor == 1


# --- 4: a turn that fails after it saved a stage with no session of its own -----

class Raising(FakeNotifier):
    def send(self, template, **ctx):
        if template == "pr_opened":
            raise RuntimeError("telegram down")
        return super().send(template, **ctx)


def test_a_turn_that_fails_after_it_saved_pr_open_stays_resumable(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    wt = make_task(c, issue=42, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "done",
         "note": "https://github.com/x/y/pull/7"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42}), notifier=Raising()))
    t = saved(c)
    assert t.stage is Stage.FAILED and resumable_crash(t)
    assert t.crashed_stage == "address-review" and t.pr_number == 7


def test_a_turn_that_fails_after_it_saved_the_spec_gate_stays_resumable(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    wt = make_task(c, issue=42, stage=Stage.SPEC)
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-review", "artifact": "spec.md",
         "track": "standard"}))

    def boom(**kw):
        raise RuntimeError("git push failed hard")
    monkeypatch.setattr(main.spec_publish, "ensure_published", boom)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = saved(c)
    assert t.stage is Stage.FAILED and t.crashed_stage == "spec"


# --- 5: a task from before tickets (ticket_count 0) -------------------------------

def test_a_task_with_no_ticket_set_keeps_its_pick_while_review_waits(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    pick = "anthropic/claude-sonnet-5@medium"
    wt = make_task(c, issue=42, track="standard", picks={"implement": pick},
                   implement_providers=["anthropic"])
    assert step(c, wt, IMPL_DONE).spawned == []
    assert raw(c)["picks"] == {"implement": pick}
    sess = wake(c)
    assert sess.spawned == []
    assert [(r[2], r[3]) for r in sess.resumed] == [
        ("anthropic/claude-sonnet-5", "medium")]


# --- 6: the label is the stage of the next launch ------------------------------------

def test_shown_stage():
    assert shown_stage(task(ticket_cursor=2, ticket_count=2)) == "review"
    assert shown_stage(task(ticket_cursor=0, ticket_count=2)) == "implement"
    assert shown_stage(task(ticket_cursor=1, ticket_count=2)) == "implement"
    assert shown_stage(task()) == "implement"
    assert shown_stage(task(Stage.PR_OPEN)) == "pr-open"
    assert shown_stage(task(Stage.FAILED, crashed_stage="implement")) == "failed"


def test_the_status_line_labels_the_review_that_waits(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", [()])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    step(c, wt, IMPL_DONE)
    assert f"— review [{ASTRA}@medium]" in main._status_lines(c)[0]


def test_the_status_line_labels_ticket_1_that_waits_implement(
        tmp_path, monkeypatch):
    c, wt, _ = accepted(tmp_path, monkeypatch)
    assert f"— implement [{ASTRA}@medium]" in main._status_lines(c)[0]


def test_the_card_labels_the_launch_that_waits(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic(), "openai": openai()})
    fake.tasks_list = [
        webfakes.make_task(issue=7, track="standard", ticket_cursor=2,
                           ticket_count=2, implement_providers=["openai"]),
        webfakes.make_task(issue=8, track="standard", ticket_cursor=0,
                           ticket_count=2)]
    got = cards(client)
    assert (got[7]["stage"], got[7]["model"]) == ("review",
                                                  "anthropic/claude-opus-5")
    assert (got[8]["stage"], got[8]["model"]) == ("implement",
                                                  "openai/gpt-sol")


# --- 7: one models.log line per launch, with the launched stage -----------------------

def test_a_wake_between_tickets_logs_the_ticket_launch_once(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), *DENY_FRONTEND))
    step(c, wt, IMPL_DONE)
    usage_now(monkeypatch, ahead())
    n = len(log_lines(wt))
    wake(c)
    assert log_lines(wt)[n:] == [f"implement anthropic/{FABLE}@medium"]


def test_a_wake_after_the_last_ticket_logs_the_review_launch(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", [()])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    step(c, wt, IMPL_DONE)
    usage_now(monkeypatch, ahead())
    n = len(log_lines(wt))
    wake(c)
    assert log_lines(wt)[n:] == [f"review {ASTRA}@medium"]
    assert json.loads((wt / ".agent" / "stage.json").read_text())["stage"] == "review"


def test_a_wake_of_a_ticket_in_progress_still_logs_its_resume(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()])
    step(c, wt, PLAN_DONE)
    n = len(log_lines(wt))
    wake(c)
    assert log_lines(wt)[n:] == [f"implement {ASTRA}@medium"]


# --- 8: one place for "what comes after this ticket" ------------------------------------

def test_after_ticket_and_the_machine_agree():
    def t(cursor, count):
        return task(ticket_cursor=cursor, ticket_count=count,
                    picks={"implement": f"{ASTRA}@medium"})
    done = StageSignal("implement", "done", note="n")
    assert after_ticket(t(1, 3)) == ("implement", 2)
    assert next_actions(t(1, 3), done, True) == [StartTicket(2, 3)]
    assert after_ticket(t(3, 3)) == ("review", 0)
    assert next_actions(t(3, 3), done, True) == [
        SpawnStage(Stage.REVIEW), Notify("review_started", "n")]
    assert after_ticket(t(0, 0)) == ("review", 0)
