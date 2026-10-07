"""Ticket 06, fix round 1: the next launch (stage, ticket) is read from the
persisted state, never inferred from a missing pick. A legacy state with no
pick is in the middle of its ticket (A); the pass writes from the saved
state after the boundary (B); an accepted ticket set and a done last ticket
are persisted before the next launch is admitted (C); a pending override
wins over the park (D); the crash repro names the crashed session's runtime
(E); the prompt texts (F); an attach wake between tickets (G)."""
import json
from dataclasses import replace

import dispatcher.main as main
from dispatcher import execution_overrides, failures, intents, state
from dispatcher.machine import Notify, StartTicket, next_actions
from dispatcher.models import ticket_tracks_text
from dispatcher.state import (PARK_HUMAN, PARK_WAKE, Stage, StageSignal,
                              TaskState, launch_ticket, launch_track, load,
                              next_launch, next_stage, save)
from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, deps,
                             make_task)
from tests.test_pinned_tracks_order import (ASTRA, FABLE, OPUS, SOL, ahead,
                                            deny, launched, make_cfg, policy)
from tests.test_pinned_tracks_ticket_tracks import (IMPL_DONE, PLAN_DONE,
                                                    setup, step, usage_now,
                                                    write_ticket)
from tests.test_web_pinned_tracks import (HEADERS, anthropic, cards, detail,
                                          models, openai, rig)
from tests import webfakes

DENY_FRONTEND = (FABLE, OPUS)


def saved(c):
    return load(c.state_dir, "portfolio_eval", 42)


def raw(c):
    return json.loads(state._path(c.state_dir, "portfolio_eval", 42).read_text())


def wake(c, **kw):
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, **kw))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    return sess


def resume_intent(c, sess=None):
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    sess = sess or FakeSessions()
    main.run_pass(c, deps(FakeGitHub(), sess))
    return sess


def task(stage=Stage.IMPLEMENT, track="architecture", **kw):
    return TaskState(issue=42, target="t", stage=stage, slot=1, worktree="",
                     branch="b", title="", updated_at="u", track=track, **kw)


# --- the one answer -------------------------------------------------------------

PICK = {"implement": f"{ASTRA}@medium"}


def test_next_launch_of_each_implement_state():
    assert next_launch(task(ticket_cursor=2, ticket_count=3,
                            picks=PICK)) == ("implement", 2)
    assert next_launch(task(ticket_cursor=2, ticket_count=3,
                            ticket_without_pick=True)) == ("implement", 2)
    assert next_launch(task(ticket_cursor=2, ticket_count=3)) == ("implement", 3)
    assert next_launch(task(ticket_cursor=0, ticket_count=3)) == ("implement", 1)
    assert next_launch(task(ticket_cursor=3, ticket_count=3)) == ("review", 0)
    assert next_launch(task(Stage.PLAN)) == ("plan", 0)
    assert next_launch(task(Stage.PR_OPEN)) == ("address-review", 0)
    crashed = task(Stage.FAILED, crashed_stage="implement", ticket_cursor=3,
                   ticket_count=3)
    assert next_launch(crashed) == ("review", 0)
    assert (next_stage(crashed), launch_ticket(crashed)) == ("review", 0)


def test_the_launch_track_is_the_next_launchs():
    between = task(ticket_cursor=1, ticket_count=3, ticket_tracks={2: "frontend"})
    assert launch_track(between, policy()) == "frontend"
    assert launch_track(replace(between, picks=PICK), policy()) == "architecture"
    last = task(ticket_cursor=2, ticket_count=2, ticket_tracks={2: "frontend"})
    assert launch_track(last, policy()) == "architecture"       # review


def test_a_state_file_from_before_the_field_marks_a_ticket_without_a_pick(
        tmp_path):
    def read(stage, picks, **over):
        save(tmp_path, task(stage, ticket_cursor=2, ticket_count=3, picks=picks))
        p = state._path(tmp_path, "t", 42)
        doc = json.loads(p.read_text())
        del doc["ticket_without_pick"]
        p.write_text(json.dumps({**doc, **over}))
        return load(tmp_path, "t", 42)
    assert read(Stage.IMPLEMENT, {}).ticket_without_pick
    assert read(Stage.FAILED, {}, crashed_stage="implement").ticket_without_pick
    assert not read(Stage.IMPLEMENT, PICK).ticket_without_pick
    assert not read(Stage.PLAN, {}).ticket_without_pick
    save(tmp_path, task(ticket_cursor=2, ticket_count=3))    # new shape
    assert not load(tmp_path, "t", 42).ticket_without_pick


# --- A: a legacy state with no pick is in the middle of its ticket -------------

def legacy(tmp_path, monkeypatch, **kw):
    """A state file from before picks existed: ticket 2 of 3 in progress."""
    c = make_cfg(tmp_path, monkeypatch, ahead())
    wt = make_task(c, issue=42, track="architecture", ticket_cursor=2,
                   ticket_count=3, picks={}, **kw)
    for n in (1, 2, 3):
        write_ticket(wt, n)
    p = state._path(c.state_dir, "portfolio_eval", 42)
    doc = json.loads(p.read_text())
    for key in ("implement_providers", "ticket_tracks", "ticket_without_pick"):
        doc.pop(key, None)
    p.write_text(json.dumps(doc))
    return c, wt


def test_a_wake_of_a_legacy_task_continues_its_ticket(tmp_path, monkeypatch):
    c, wt = legacy(tmp_path, monkeypatch, park=PARK_WAKE)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == [] and len(sess.resumed) == 1
    assert saved(c).ticket_cursor == 2


def test_a_crash_resume_of_a_legacy_task_respawns_its_ticket(
        tmp_path, monkeypatch):
    c, wt = legacy(tmp_path, monkeypatch, stage=Stage.FAILED,
                   crashed_stage="implement",
                   updated_at="2026-10-01T11:00:00+00:00")
    sess = resume_intent(c)
    assert [s[1] for s in sess.spawned] == ["implement"]
    assert "02-t2.md" in sess.spawned[0][3]
    assert saved(c).ticket_cursor == 2


# --- B: a failed start of the next ticket ----------------------------------------

def test_a_failed_ticket_start_does_not_put_the_done_tickets_pick_back(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    (wt / ".agent" / "stage.json").write_text(json.dumps(IMPL_DONE))
    main.run_pass(c, deps(sess=FakeSessions(alive={42}, spawn_raises=[42])))
    t = saved(c)
    assert (t.stage, t.crashed_stage, t.ticket_cursor) == (
        Stage.FAILED, "implement", 1)
    assert "implement" not in raw(c)["picks"]
    sess = resume_intent(c)
    assert "02-t2.md" in sess.spawned[0][3]
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).ticket_cursor == 2


# --- C1: the accepted ticket set is persisted before ticket 1 is admitted --------

def accepted_and_waiting(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [("frontend",), ()],
                  usages=deny(ahead(), *DENY_FRONTEND))
    sess = step(c, wt, PLAN_DONE)
    assert sess.spawned == [] and sess.resumed == []
    return c, wt


def test_an_accepted_set_is_saved_although_ticket_1_waits(tmp_path, monkeypatch):
    c, wt = accepted_and_waiting(tmp_path, monkeypatch)
    t = saved(c)
    assert (t.stage, t.ticket_cursor, t.ticket_count) == (Stage.IMPLEMENT, 0, 2)
    assert t.ticket_tracks == {1: "frontend"}
    assert next_launch(t) == ("implement", 1)


def test_an_accepted_set_is_not_read_again(tmp_path, monkeypatch):
    c, wt = accepted_and_waiting(tmp_path, monkeypatch)
    write_ticket(wt, 1, "standard")          # would be refused; would reroute
    usage_now(monkeypatch, ahead())
    sess = step(c, wt, PLAN_DONE)
    assert sess.resumed == []
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).plan_retries == 0


def test_the_implement_started_ping_comes_when_ticket_1_starts():
    t = task(ticket_cursor=0, ticket_count=2)
    assert next_actions(t, StageSignal("plan", "done"), True) == [
        StartTicket(1, 2), Notify("implement_started", "2 ticket(s)")]
    later = replace(t, ticket_cursor=1)
    assert next_actions(later, StageSignal("implement", "done"), True) == [
        StartTicket(2, 2)]


def test_the_status_line_names_ticket_1s_entry(tmp_path, monkeypatch):
    c, wt = accepted_and_waiting(tmp_path, monkeypatch)
    assert f"implement [anthropic/{FABLE}@medium]" in main._status_lines(c)[0]


def test_a_wake_of_an_accepted_task_starts_ticket_1(tmp_path, monkeypatch):
    c, wt = accepted_and_waiting(tmp_path, monkeypatch)
    usage_now(monkeypatch, ahead())
    sess = wake(c)
    assert sess.resumed == [] and "01-t1.md" in sess.spawned[0][3]
    assert launched(sess) == [(f"anthropic/{FABLE}", "medium")]
    assert saved(c).ticket_cursor == 1


def test_an_override_of_another_provider_runs_ticket_1(tmp_path, monkeypatch):
    c, wt = accepted_and_waiting(tmp_path, monkeypatch)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=SOL, bypass_usage=False))
    assert launched(step(c, wt, PLAN_DONE)) == [(SOL, "")]


def waiting_card(tmp_path, **kw):
    fake, client = rig(tmp_path, {"anthropic": anthropic("Fable", "Opus"),
                                  "openai": openai()})
    fake.tasks_list = [webfakes.make_task(
        issue=7, track="architecture", stage=Stage.IMPLEMENT,
        picks={"plan": "openai/gpt-astra@high"}, **kw)]
    return fake, client


def test_the_console_shows_ticket_1_of_an_accepted_task(tmp_path):
    fake, client = waiting_card(tmp_path, ticket_cursor=0, ticket_count=2,
                                ticket_tracks={1: "frontend"})
    card = cards(client)[7]
    assert card["model"].endswith("claude-fable-5-1")
    assert card["pinned_track"] == "frontend"
    admission = detail(client, 7)["card"]["admission"]
    assert models(admission)["openai/gpt-sol"] is True
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "anthropic/claude-sonnet-5"})
    assert r.status_code == 200


# --- C2: the last ticket is done before review is admitted -----------------------

def review_waits(tmp_path, monkeypatch):
    """A standard task whose one ticket ran on sonnet; review (astra, opus
    with anthropic moved back) is denied."""
    c, wt = setup(tmp_path, monkeypatch, "standard", [()])
    assert launched(step(c, wt, PLAN_DONE)) == [("anthropic/claude-sonnet-5",
                                                 "medium")]
    usage_now(monkeypatch, deny(ahead(), "gpt-astra", OPUS))
    sess = step(c, wt, IMPL_DONE)
    assert sess.spawned == [] and sess.resumed == []
    return c, wt


def test_the_last_tickets_pick_is_dropped_although_review_waits(
        tmp_path, monkeypatch):
    c, wt = review_waits(tmp_path, monkeypatch)
    assert "implement" not in raw(c)["picks"]
    t = saved(c)
    assert t.stage is Stage.IMPLEMENT and next_launch(t) == ("review", 0)
    assert f"[{ASTRA}@medium]" in main._status_lines(c)[0]


def test_a_wake_after_the_last_ticket_starts_review(tmp_path, monkeypatch):
    c, wt = review_waits(tmp_path, monkeypatch)
    usage_now(monkeypatch, ahead())
    sess = wake(c)
    assert sess.resumed == []
    assert [(s[1], s[2]) for s in sess.spawned] == [("review", ASTRA)]
    assert saved(c).stage is Stage.REVIEW


def test_a_crash_resume_after_the_last_ticket_starts_review(
        tmp_path, monkeypatch):
    c, wt = review_waits(tmp_path, monkeypatch)
    save(c.state_dir, replace(saved(c), stage=Stage.FAILED,
                              crashed_stage="implement",
                              updated_at="2026-10-01T11:00:00+00:00"))
    usage_now(monkeypatch, ahead())
    sess = resume_intent(c)
    assert [(s[1], s[2]) for s in sess.spawned] == [("review", ASTRA)]


def test_the_console_names_review_after_the_last_ticket(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic(), "openai": openai()})
    fake.tasks_list = [webfakes.make_task(
        issue=7, track="standard", stage=Stage.IMPLEMENT,
        ticket_cursor=2, ticket_count=2, ticket_tracks={2: "frontend"},
        implement_providers=["openai"])]
    card = cards(client)[7]
    assert card["model"] == "anthropic/claude-opus-5"   # review, openai back
    assert card["pinned_track"] == ""


# --- D: a pending override wins over the park -------------------------------------

def test_a_pending_override_starts_a_ticket_whose_track_is_not_pinned(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=SOL, bypass_usage=False))
    c2 = replace(c, models=policy(pinned=["security", "architecture"]))
    assert launched(step(c2, wt, IMPL_DONE)) == [(SOL, "")]
    t = saved(c)
    assert not t.park and t.ticket_cursor == 2
    assert execution_overrides.load(c.state_dir, "portfolio_eval", 42) is None


# --- E: the crash repro names the crashed session's runtime -----------------------

def test_a_crash_between_tickets_names_the_last_sessions_runtime(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)],
                  usages=ahead())
    step(c, wt, PLAN_DONE)                      # ticket 1 on openai/gpt-astra
    usage_now(monkeypatch, deny(ahead(), *DENY_FRONTEND))
    step(c, wt, IMPL_DONE)                      # ticket 2 waits, no pick
    reports = []
    monkeypatch.setattr(failures, "report_failure",
                        lambda cfg, deps, rep, **kw: reports.append(rep))
    main._report_session_crash(c, deps(), c.targets[0], saved(c), False)
    assert "codex" in reports[0].repro and "claude" not in reports[0].repro


# --- F: the prompt texts ------------------------------------------------------------

def test_the_plan_prompt_says_what_a_track_line_may_not_be():
    text = ticket_tracks_text(policy(), "standard")
    para = text.split("\n\n")[0]
    assert "No other line of a ticket may start with `Track:`" in para
    assert "omit the line" in para and "`Track: none`" in para


def test_the_plan_retry_says_what_a_track_line_may_not_be(tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "standard", [("standard",)])
    text = step(c, wt, PLAN_DONE).resumed[0][1]
    assert "no other line may start with 'Track:'" in text
    assert "omit the line" in text


# --- G: an attach wake between two tickets ---------------------------------------------

def test_an_attach_wake_between_tickets_tells_the_new_session_to_wait(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), *DENY_FRONTEND))
    step(c, wt, IMPL_DONE)
    usage_now(monkeypatch, ahead())
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, hold_for_attach=True))
    sess, notifier = FakeSessions(), FakeNotifier()
    main.run_pass(c, deps(sess=sess, notifier=notifier))
    assert "The operator is attaching to talk to you directly" in sess.spawned[0][3]
    assert "resumed_for_attach" in notifier.sent
    assert not saved(c).hold_for_attach
