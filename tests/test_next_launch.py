"""The next launch is read from the persisted state: the one answer the
dispatcher, the status line and the console share. The track it reads is the
task track; after a done implement session it is review, also while review
waits; a turn that fails after it saved a stage with no session of its own
stays resumable; an attach wake queues its notice once for every resume
path."""
import json
from dataclasses import replace

import dispatcher.main as main
from dispatcher import state
from dispatcher.models import parse_policy
from dispatcher.state import (NO_SLOT, PARK_WAKE, Stage, launch_entries,
                              launch_track, next_stage, resumable_crash)
from tests import webfakes
from tests.pinned import (ASTRA, FABLE, IMPL_DONE, OPUS, PLAN_DONE, ahead,
                          anthropic, cards, deny, launched, make_cfg, openai,
                          policy, policy_raw, rig, saved, setup, step, task,
                          usage_now)
from tests.test_main import (PLAN_SUMMARY, SPEC_PATH, FakeNotifier,
                             FakeSessions, deps, make_task, write_tickets)

PICK = {"implement": f"{ASTRA}@medium"}


# --- the track and the stage ------------------------------------------------------

def test_the_launch_track_is_the_task_track():
    assert launch_track(task(), policy()) == "architecture"
    assert launch_track(task(track=""), policy()) == "standard"     # untracked
    assert launch_track(task(track="gone"), policy()) == ""


def test_the_next_stage_of_an_implement_task():
    assert next_stage(task()) == "implement"                        # not started
    assert next_stage(task(picks=PICK, implement_providers=["openai"])) == "implement"
    assert next_stage(task(implement_providers=["openai"])) == "review"   # done


def test_a_task_with_no_track_is_untracked_work_for_the_status_line_too(
        tmp_path, monkeypatch):
    """The status line names the entry the dispatcher launches: the task has
    no track (claimed before tracks existed, parked ever since), the
    untracked track is pinned, mode `openai` does not reorder it."""
    c = make_cfg(tmp_path, monkeypatch, ahead(), pinned=["standard"],
                 mode="openai")
    make_task(c, issue=42, stage=Stage.PLAN, track="", park=PARK_WAKE)
    assert f"plan [anthropic/{FABLE}@medium]" in main._status_lines(c)[0]
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [(r[2], r[3]) for r in sess.resumed] == [(f"anthropic/{FABLE}", "medium")]


def test_launch_entries_reads_the_list_of_the_stage_it_is_given():
    """The drive loop starts the stage after a done one, which the saved
    state does not name yet: an approved plan reads the implement list, a
    done implement session reads the review list (with the review avoid)."""
    raw_policy = policy_raw()
    raw_policy["tracks"]["standard"]["plan"] = [f"{OPUS}@low"]
    pol = parse_policy(raw_policy)
    gate = task(Stage.AWAITING_PLAN_REVIEW, track="standard")
    assert [str(e) for e in launch_entries(gate, pol, tuple)] == [
        f"anthropic/{OPUS}@low"]                                    # the plan list
    assert [str(e) for e in launch_entries(gate, pol, tuple, "implement")] == [
        str(e) for e in pol.tracks["standard"].stages["implement"]]
    done = task(implement_providers=["anthropic"])
    assert [str(e) for e in launch_entries(done, pol, tuple, "review")] == [
        f"{ASTRA}@high", f"anthropic/{FABLE}@high"]
    assert [str(e) for e in launch_entries(task(picks=PICK), pol, tuple)] == [
        f"{ASTRA}@medium", f"anthropic/{FABLE}@medium"]


# --- the label is the stage of the next launch ------------------------------------

def test_the_status_line_labels_the_review_that_waits(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", 1)
    assert launched(step(c, wt, PLAN_DONE)) == [("anthropic/claude-sonnet-5",
                                                 "medium")]
    usage_now(monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    sess = step(c, wt, IMPL_DONE)
    assert sess.spawned == [] and sess.resumed == []
    assert "implement" not in saved(c).picks          # dropped before review waits
    assert f"— review [{ASTRA}@medium]" in main._status_lines(c)[0]


def test_a_wake_of_a_done_implement_task_starts_review_afresh(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", 1)
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    step(c, wt, IMPL_DONE)
    usage_now(monkeypatch, ahead())
    state.save(c.state_dir, replace(saved(c), park=PARK_WAKE))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [s[1] for s in sess.spawned] == ["review"] and sess.resumed == []


def test_the_card_labels_the_launch_that_waits(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic(), "openai": openai()})
    fake.tasks_list = [
        webfakes.make_task(issue=7, track="standard",
                           implement_providers=["openai"]),
        webfakes.make_task(issue=8, track="standard")]
    got = cards(client)
    assert (got[7]["stage"], got[7]["model"]) == ("review",
                                                  "anthropic/claude-opus-5")
    assert (got[8]["stage"], got[8]["model"]) == ("implement",
                                                  "openai/gpt-sol")


# --- a turn that fails after it saved a stage with no session of its own -----------

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


def test_a_turn_that_fails_after_it_saved_the_plan_gate_stays_resumable(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    wt = make_task(c, issue=42, stage=Stage.PLAN, spec_path=SPEC_PATH)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "note": "plan ready",
         "artifact": PLAN_SUMMARY}))

    def boom(**kw):
        raise RuntimeError("git push failed hard")
    monkeypatch.setattr(main.spec_publish, "ensure_published", boom)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = saved(c)
    assert t.stage is Stage.FAILED and resumable_crash(t)
    assert t.crashed_stage == "plan"


class NoTail(FakeSessions):
    def capture_tail(self, target, issue, lines=25):
        raise RuntimeError("herdr down")


def test_a_crash_whose_report_fails_stays_resumable(tmp_path, monkeypatch):
    """The implement session died and the crash was saved as resumable; the
    report then raises. The second failure keeps the recorded crashed stage."""
    c, wt = setup(tmp_path, monkeypatch, "architecture", 2)
    step(c, wt, PLAN_DONE)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    main.run_pass(c, deps(sess=NoTail()))           # no live session
    t = saved(c)
    assert t.stage is Stage.FAILED and t.crashed_stage == "implement"
    assert resumable_crash(t)


class Corrupting(FakeSessions):
    """The turn leaves the state file unreadable, then raises."""

    def __init__(self, path):
        super().__init__()
        self.path = path

    def is_alive(self, target, issue):
        self.path.write_text("{not json")
        raise RuntimeError("boom")


def test_a_crash_over_an_unreadable_state_file_does_not_stop_the_pass(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    make_task(c, issue=42, track="architecture", picks=PICK)
    make_task(c, issue=43, track="architecture", picks=PICK, park=PARK_WAKE)
    sess = Corrupting(state._path(c.state_dir, "portfolio_eval", 42))
    main.run_pass(c, deps(sess=sess))
    t = saved(c)
    assert t.stage is Stage.FAILED and t.crashed_stage == "implement"
    assert len(sess.resumed) == 1                   # task 43 was still reached


# --- the attach notice -------------------------------------------------------------

def test_an_attach_wake_of_a_pr_open_task_tells_the_new_session_to_wait(
        tmp_path, monkeypatch):
    """One place queues the attach notice for every resume path."""
    c = make_cfg(tmp_path, monkeypatch, ahead())
    make_task(c, issue=42, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12,
              track="architecture", park=PARK_WAKE, hold_for_attach=True)
    sess, notifier = FakeSessions(), FakeNotifier()
    main.run_pass(c, deps(sess=sess, notifier=notifier))
    assert sess.spawned[0][1] == "address-review"
    assert "The operator is attaching to talk to you directly" in sess.spawned[0][3]
    assert "resumed_for_attach" in notifier.sent
