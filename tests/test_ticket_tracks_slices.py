"""Unit slices for ticket 06 of pinned-tracks that the acceptance tests leave
out: the state round trip of the ticket tracks, the track a launch reads its
list from, the ticket set the machine hands over, the two state writes at a
ticket boundary, and the status line's entry for PR feedback."""
import json
from dataclasses import replace

import dispatcher.main as main
from dispatcher import state
from dispatcher.machine import Notify, RetryStage, StartTicket, next_actions
from dispatcher.models import parse_policy
from dispatcher.state import (PARK_WAKE, Stage, StageSignal, TaskState,
                              launch_track, load, save)
from tests.test_main import GOOD_TICKET, FakeSessions, deps, make_task
from tests.test_pinned_tracks_order import (ASTRA, FABLE, OPUS, SOL, ahead,
                                            deny, launched, make_cfg, policy)
from tests.test_pinned_tracks_ticket_tracks import (IMPL_DONE, PLAN_DONE,
                                                    setup, step, usage_now,
                                                    write_ticket)

POLICY = policy()


def task(stage=Stage.IMPLEMENT, track="architecture", **kw):
    return TaskState(issue=42, target="t", stage=stage, slot=1, worktree="",
                     branch="b", title="", updated_at="u", track=track, **kw)


# --- state ------------------------------------------------------------------

def test_ticket_tracks_round_trip_with_int_keys(tmp_path):
    save(tmp_path, task(ticket_tracks={2: "frontend", 11: "architecture"}))
    assert load(tmp_path, "t", 42).ticket_tracks == {2: "frontend",
                                                    11: "architecture"}


def test_a_state_file_from_before_ticket_tracks_reads_as_none(tmp_path):
    save(tmp_path, task())
    p = state._path(tmp_path, "t", 42)
    raw = json.loads(p.read_text())
    del raw["ticket_tracks"]
    p.write_text(json.dumps(raw))
    assert load(tmp_path, "t", 42).ticket_tracks == {}


# --- the track a launch reads its list from ----------------------------------

def test_between_tickets_the_next_ticket_names_the_track():
    t = task(ticket_cursor=1, ticket_tracks={2: "frontend"})
    assert launch_track(t, "implement", POLICY) == "frontend"


def test_a_ticket_in_progress_names_the_track():
    picks = {"implement": f"{ASTRA}@medium"}
    t = task(ticket_cursor=1, ticket_tracks={2: "frontend"}, picks=picks)
    assert launch_track(t, "implement", POLICY) == "architecture"
    assert launch_track(replace(t, ticket_cursor=2), "implement",
                        POLICY) == "frontend"


def test_a_ticket_without_a_track_uses_the_task_track():
    t = task(ticket_cursor=2, ticket_tracks={2: "frontend"})
    assert launch_track(t, "implement", POLICY) == "architecture"


def test_only_an_implement_launch_reads_a_ticket_track():
    t = task(ticket_cursor=1, ticket_tracks={2: "frontend"})
    for stage in ("spec", "plan", "review", "address-review"):
        assert launch_track(t, stage, POLICY) == "architecture"


def test_a_ticket_track_no_longer_pinned_gives_no_track():
    t = task(ticket_cursor=1, ticket_tracks={2: "frontend"})
    assert launch_track(t, "implement",
                        policy(pinned=["security", "architecture"])) == ""


def test_a_task_track_the_policy_does_not_know_gives_no_track():
    assert launch_track(task(track="legacy"), "implement", POLICY) == ""


# --- machine ------------------------------------------------------------------

def tickets(tmp_path, *lines):
    d = tmp_path / ".agent" / "tickets"
    d.mkdir(parents=True)
    for n, line in enumerate(lines, 1):
        (d / f"{n:02d}-t{n}.md").write_text(GOOD_TICKET + f"\n{line}\n")
    return replace(task(Stage.PLAN), worktree=str(tmp_path))


def test_plan_done_hands_over_the_ticket_tracks(tmp_path):
    t = tickets(tmp_path, "", "Track: frontend")
    acts = next_actions(t, StageSignal("plan", "done"), True,
                        ticket_tracks=("architecture", "frontend"))
    assert acts == [StartTicket(1, 2, {2: "frontend"}),
                    Notify("implement_started", "2 ticket(s)")]


def test_plan_done_with_a_track_no_ticket_may_name_retries(tmp_path):
    t = tickets(tmp_path, "", "Track: frontend")
    (act,) = next_actions(t, StageSignal("plan", "done"), True)
    assert isinstance(act, RetryStage) and "ticket 02" in act.reason


def test_implement_done_keeps_the_accepted_ticket_tracks():
    t = task(ticket_cursor=1, ticket_count=3, ticket_tracks={3: "frontend"})
    assert next_actions(t, StageSignal("implement", "done"), True) == [
        StartTicket(2, 3, {3: "frontend"})]


# --- the ticket boundary ------------------------------------------------------

def test_a_done_ticket_drops_its_pick_before_the_next_one_is_chosen(
        tmp_path, monkeypatch):
    """Two writes at a ticket boundary: the pick goes on its own, so a next
    ticket that must wait leaves no implement pick and the providers that
    ran tickets as they were."""
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    writes = []
    real = main.save
    monkeypatch.setattr(main, "save", lambda d, ts: (
        writes.append(ts.picks.get("implement", "")), real(d, ts))[1])
    step(c, wt, IMPL_DONE)
    assert writes == ["", f"anthropic/{FABLE}@medium"]
    assert load(c.state_dir, "portfolio_eval", 42).implement_providers == [
        "openai", "anthropic"]


def test_a_waiting_ticket_keeps_the_recorded_providers(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), FABLE, OPUS))
    step(c, wt, IMPL_DONE)
    step(c, wt, IMPL_DONE)   # a second read of the saved state adds nothing
    raw = json.loads(state._path(c.state_dir, "portfolio_eval", 42).read_text())
    assert raw["implement_providers"] == ["openai"]
    assert "implement" not in raw["picks"]


def test_review_starts_without_the_last_tickets_pick(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [()])
    step(c, wt, PLAN_DONE)
    step(c, wt, IMPL_DONE)
    raw = json.loads(state._path(c.state_dir, "portfolio_eval", 42).read_text())
    assert raw["stage"] == "review" and "implement" not in raw["picks"]


def test_a_ticket_whose_track_is_not_pinned_parks_with_no_implement_pick(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    step(unpinned(c), wt, IMPL_DONE)
    t = saved(c)
    assert "implement" not in t.picks and "pinned" in t.park_note


# --- a wake between two tickets -------------------------------------------------

def unpinned(c):
    return replace(c, models=policy(pinned=["security", "architecture"]))


def saved(c):
    return load(c.state_dir, "portfolio_eval", 42)


def wake(c, **kw):
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, **kw))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    return sess


def test_a_wake_between_tickets_starts_the_next_ticket_afresh(
        tmp_path, monkeypatch):
    """No ticket is in progress, so no session is continued and no pick is
    put back for the ticket before: the next ticket starts on its own list."""
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",), ()])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), FABLE, OPUS))
    step(c, wt, IMPL_DONE)              # ticket 2 waits; the operator parks it
    usage_now(monkeypatch, ahead())
    sess = wake(c)
    assert sess.resumed == []
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    t = saved(c)
    assert t.ticket_cursor == 2 and not t.park
    assert t.picks["implement"] == f"anthropic/{FABLE}@medium"
    assert t.implement_providers == ["openai", "anthropic"]


def test_a_resume_override_between_tickets_covers_the_next_ticket_only(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture",
                  [(), ("frontend",), ("frontend",)])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), FABLE, OPUS))
    step(c, wt, IMPL_DONE)
    assert launched(wake(c, resume_model_override=SOL)) == [(SOL, "")]
    assert saved(c).ticket_cursor == 2
    assert step(c, wt, IMPL_DONE).spawned == []     # ticket 3 waits again
    assert "implement" not in saved(c).picks


def test_a_wake_of_a_ticket_whose_track_is_not_pinned_waits_for_the_pin(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    step(unpinned(c), wt, IMPL_DONE)
    save(c.state_dir, replace(saved(c), park=PARK_WAKE))
    sess = FakeSessions()
    main.run_pass(unpinned(c), deps(sess=sess))
    assert sess.spawned == [] and sess.resumed == []
    assert saved(c).park == PARK_WAKE
    main.run_pass(c, deps(sess=sess))               # pinned again
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).ticket_cursor == 2


def test_a_wake_of_a_ticket_in_progress_continues_its_session(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    sess = wake(c)
    assert sess.spawned == []
    assert [(r[2], r[3]) for r in sess.resumed] == [(ASTRA, "medium")]


def test_a_new_ticket_set_replaces_the_whole_copy(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    wt = make_task(c, issue=42, stage=Stage.PLAN, track="architecture",
                   ticket_tracks={1: "frontend", 5: "frontend"})
    write_ticket(wt, 1)
    write_ticket(wt, 2, "frontend")
    step(c, wt, PLAN_DONE)
    assert load(c.state_dir, "portfolio_eval", 42).ticket_tracks == {
        2: "frontend"}


# --- status line ----------------------------------------------------------------

def test_display_entry_of_pr_feedback_is_the_feedback_pick(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    t = task(Stage.ADDRESS_REVIEW, picks={"implement": f"{SOL}@medium",
                                          "feedback": f"{ASTRA}@medium"})
    assert main._display_entry(c, None, t) == f"{ASTRA}@medium"
    assert main._display_entry(c, None, replace(t, stage=Stage.PR_OPEN)) == (
        f"{ASTRA}@medium")


def test_display_entry_of_pr_feedback_with_no_pick_ignores_ticket_tracks(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead())
    t = task(Stage.PR_OPEN, track="standard", ticket_cursor=1,
             ticket_tracks={2: "frontend"})
    assert main._display_entry(c, None, t) == "anthropic/claude-sonnet-5@medium"
