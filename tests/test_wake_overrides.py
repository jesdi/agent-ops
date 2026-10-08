"""One-shot overrides on the wake path. A stored execution override means
"the next launch runs on this model", also when the next launch is a wake;
a resume that names its own model wins; either way the stored one is used
up. A wake that goes back to the operator keeps none of its one-shot
fields, and the attach notice is only said for a launch that happened."""
from dataclasses import replace

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides, messages
from dispatcher.state import PARK_HUMAN, PARK_WAKE, save
from tests import webfakes
from tests.pinned import (ASTRA, DENY_FRONTEND, FABLE, IMPL_DONE, OPUS,
                          PLAN_DONE, SOL, ahead, anthropic, cards, deny,
                          detail, launched, openai, rig, saved, setup, step,
                          unpinned, usage_now, wake)
from tests.test_main import FakeNotifier, FakeSessions, deps

LUNA = "openai/gpt-luna"
ATTACHING = "The operator is attaching to talk to you directly"


def store(c, model, bypass=False):
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=model, bypass_usage=bypass))


def stored(c):
    return execution_overrides.load(c.state_dir, "portfolio_eval", 42)


def between_tickets(tmp_path, monkeypatch, cfg=None):
    """Ticket 1 done on openai; ticket 2 (ticket track frontend) not started."""
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ("frontend",)])
    step(c, wt, PLAN_DONE)
    usage_now(monkeypatch, deny(ahead(), *DENY_FRONTEND))
    step(c, wt, IMPL_DONE)
    usage_now(monkeypatch, ahead())
    return c, wt


# --- the matrix: stored override X, resume model Y ---------------------------

@pytest.mark.parametrize("x, y, model", [
    ("", "", (f"anthropic/{FABLE}", "medium")),      # the ticket's own list
    (SOL, "", (SOL, "")),                            # X is the next launch
    ("", f"anthropic/{OPUS}", (f"anthropic/{OPUS}", "")),
    (SOL, f"anthropic/{OPUS}", (f"anthropic/{OPUS}", "")),   # Y wins
])
def test_a_wake_between_tickets(tmp_path, monkeypatch, x, y, model):
    c, wt = between_tickets(tmp_path, monkeypatch)
    if x:
        store(c, x)
    sess = wake(c, resume_model_override=y)
    assert launched(sess) == [model] and sess.resumed == []
    assert saved(c).ticket_cursor == 2
    assert stored(c) is None                          # used up either way


@pytest.mark.parametrize("x, y, model", [
    ("", "", (ASTRA, "medium")),                     # the ticket's pick
    (SOL, "", (SOL, "")),
    ("", LUNA, (LUNA, "")),
    (SOL, LUNA, (LUNA, "")),
    (f"anthropic/{OPUS}", "", (ASTRA, "medium")),    # another provider: dropped
])
def test_a_wake_of_a_parked_ticket_in_progress(tmp_path, monkeypatch, x, y, model):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()])
    step(c, wt, PLAN_DONE)                           # ticket 1 on openai/gpt-astra
    if x:
        store(c, x)
    sess = wake(c, resume_model_override=y)
    assert [(r[2], r[3]) for r in sess.resumed] == [model]
    assert sess.spawned == [] and saved(c).ticket_cursor == 1
    assert stored(c) is None


def test_a_stored_override_on_a_wake_is_still_gated(tmp_path, monkeypatch):
    c, wt = between_tickets(tmp_path, monkeypatch)
    store(c, SOL)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    assert wake(c).spawned == []
    assert saved(c).park == PARK_WAKE and stored(c).model == SOL


def test_a_stored_bypass_on_a_wake_skips_the_gate(tmp_path, monkeypatch):
    c, wt = between_tickets(tmp_path, monkeypatch)
    store(c, SOL, bypass=True)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    assert launched(wake(c)) == [(SOL, "")]


# --- a ticket whose track is still not pinned --------------------------------

def test_a_stored_override_starts_a_woken_ticket_whose_track_is_not_pinned(
        tmp_path, monkeypatch):
    c, wt = between_tickets(tmp_path, monkeypatch)
    store(c, SOL)
    assert launched(wake(c, unpinned(c))) == [(SOL, "")]
    assert saved(c).ticket_cursor == 2 and stored(c) is None


def test_a_denied_override_of_a_woken_unpinned_ticket_waits(
        tmp_path, monkeypatch):
    """Not parked (the override is the launch) and not started: it waits as
    any denied launch does, the override kept."""
    c, wt = between_tickets(tmp_path, monkeypatch)
    store(c, SOL)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    notifier = FakeNotifier()
    save(c.state_dir, replace(saved(c), park=PARK_WAKE))
    sess = FakeSessions()
    main.run_pass(unpinned(c), deps(sess=sess, notifier=notifier))
    assert sess.spawned == [] and "parked_question" not in notifier.sent
    assert saved(c).park == PARK_WAKE and stored(c).model == SOL


def test_the_console_shows_the_wait_of_a_denied_stored_override(tmp_path):
    """The card names the model the dispatcher will launch, the stored
    override's, and its wait reason says that this model is denied."""
    fake, client = rig(tmp_path, {"anthropic": anthropic(),
                                  "openai": openai(0.99)})
    fake.tasks_list = [webfakes.make_task(
        issue=7, track="architecture", park=PARK_WAKE, ticket_cursor=1,
        ticket_count=2, ticket_tracks={2: "frontend"})]
    fake.set_execution_override("alpha", 7, model=SOL, bypass_usage=False)
    card = cards(client)[7]
    assert card["model"] == SOL and card["pinned_track"] == ""
    admission = detail(client, 7)["card"]["admission"]
    assert admission["requested"] == {**admission["requested"], "model": SOL,
                                      "admitted": False}
    fake.set_execution_override("alpha", 7, model=SOL, bypass_usage=True)
    assert detail(client, 7)["card"]["admission"] is None


# --- a wake that goes back to the operator ------------------------------------

def test_a_wake_that_parks_again_keeps_no_one_shot_field(tmp_path, monkeypatch):
    """An attach wake finds the ticket's track still not pinned and parks
    again. The reply that wakes it later is a plain wake: no attach notice."""
    c, wt = between_tickets(tmp_path, monkeypatch)
    wake(c, unpinned(c), hold_for_attach=True, resume_bypass_usage=True)
    t = saved(c)
    assert t.park == PARK_HUMAN
    assert (t.hold_for_attach, t.resume_model_override,
            t.resume_bypass_usage) == (False, "", False)
    notifier = FakeNotifier()
    save(c.state_dir, replace(saved(c), park=PARK_WAKE))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess, notifier=notifier))
    assert len(sess.spawned) == 1 and ATTACHING not in sess.spawned[0][3]
    assert "resumed_for_attach" not in notifier.sent


# --- the attach notice ---------------------------------------------------------

def test_a_failed_attach_resume_says_nothing_and_leaves_no_notice(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()])
    step(c, wt, PLAN_DONE)
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, hold_for_attach=True))
    notifier = FakeNotifier()
    main.run_pass(c, deps(sess=FakeSessions(resume_raises=[42]),
                          notifier=notifier))
    assert "resumed_for_attach" not in notifier.sent
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_an_attach_resume_that_starts_pings_after_the_launch(
        tmp_path, monkeypatch):
    c, wt = setup(tmp_path, monkeypatch, "architecture", [(), ()])
    step(c, wt, PLAN_DONE)
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, hold_for_attach=True))
    sess, notifier = FakeSessions(), FakeNotifier()
    main.run_pass(c, deps(sess=sess, notifier=notifier))
    assert ATTACHING in sess.resumed[0][1]
    assert notifier.sent.count("resumed_for_attach") == 1
