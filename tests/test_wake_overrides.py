"""One-shot overrides on the wake path. A stored execution override means
"the next launch runs on this model", also when the next launch is a wake;
a resume that names its own model wins; either way the stored one is used
up. The attach notice is only said for a launch that happened."""
from dataclasses import replace

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides, messages
from dispatcher.state import PARK_WAKE, save
from tests import webfakes
from tests.pinned import (ASTRA, OPUS, PLAN_DONE, SOL, ahead, anthropic,
                          cards, deny, detail, openai, rig, saved, setup, step,
                          usage_now, wake)
from tests.test_main import FakeNotifier, FakeSessions, deps

LUNA = "openai/gpt-luna"
ATTACHING = "The operator is attaching to talk to you directly"


def store(c, model, bypass=False):
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=model, bypass_usage=bypass))


def stored(c):
    return execution_overrides.load(c.state_dir, "portfolio_eval", 42)


def implementing(tmp_path, monkeypatch):
    """An approved plan: the implement session runs on openai/gpt-astra, the
    first entry of the pinned architecture track's implement list."""
    c, wt = setup(tmp_path, monkeypatch, "architecture", 2)
    step(c, wt, PLAN_DONE)
    return c, wt


def resumed(sess):
    return [(r[2], r[3]) for r in sess.resumed]


# --- the matrix: stored override X, resume model Y ---------------------------

@pytest.mark.parametrize("x, y, model", [
    ("", "", (ASTRA, "medium")),                     # the implement pick
    (SOL, "", (SOL, "")),
    ("", LUNA, (LUNA, "")),
    (SOL, LUNA, (LUNA, "")),
    (f"anthropic/{OPUS}", "", (ASTRA, "medium")),    # another provider: dropped
])
def test_a_wake_of_a_parked_implement_session(tmp_path, monkeypatch, x, y, model):
    c, wt = implementing(tmp_path, monkeypatch)
    if x:
        store(c, x)
    sess = wake(c, resume_model_override=y)
    assert resumed(sess) == [model] and sess.spawned == []
    assert stored(c) is None                          # used up either way


def test_a_stored_override_on_a_wake_is_still_gated(tmp_path, monkeypatch):
    c, wt = implementing(tmp_path, monkeypatch)
    store(c, SOL)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    sess = wake(c)
    assert sess.resumed == [] and sess.spawned == []
    assert saved(c).park == PARK_WAKE and stored(c).model == SOL


def test_a_stored_bypass_on_a_wake_skips_the_gate(tmp_path, monkeypatch):
    c, wt = implementing(tmp_path, monkeypatch)
    store(c, SOL, bypass=True)
    usage_now(monkeypatch, deny(ahead(), "gpt-sol"))
    assert resumed(wake(c)) == [(SOL, "")]


def test_the_console_shows_the_wait_of_a_denied_stored_override(tmp_path):
    """The card names the model the dispatcher will launch, the stored
    override's, and its wait reason says that this model is denied."""
    fake, client = rig(tmp_path, {"anthropic": anthropic(),
                                  "openai": openai(0.99)})
    fake.tasks_list = [webfakes.make_task(
        issue=7, track="architecture", park=PARK_WAKE)]
    fake.set_execution_override("alpha", 7, model=SOL, bypass_usage=False)
    card = cards(client)[7]
    assert card["model"] == SOL and card["pinned_track"] == ""
    admission = detail(client, 7)["card"]["admission"]
    assert admission["requested"] == {**admission["requested"], "model": SOL,
                                      "admitted": False}
    fake.set_execution_override("alpha", 7, model=SOL, bypass_usage=True)
    assert detail(client, 7)["card"]["admission"] is None


# --- the attach notice ---------------------------------------------------------

def test_a_failed_attach_resume_says_nothing_and_leaves_no_notice(
        tmp_path, monkeypatch):
    c, wt = implementing(tmp_path, monkeypatch)
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, hold_for_attach=True))
    notifier = FakeNotifier()
    main.run_pass(c, deps(sess=FakeSessions(resume_raises=[42]),
                          notifier=notifier))
    assert "resumed_for_attach" not in notifier.sent
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_an_attach_resume_that_starts_pings_after_the_launch(
        tmp_path, monkeypatch):
    c, wt = implementing(tmp_path, monkeypatch)
    save(c.state_dir, replace(saved(c), park=PARK_WAKE, hold_for_attach=True))
    sess, notifier = FakeSessions(), FakeNotifier()
    main.run_pass(c, deps(sess=sess, notifier=notifier))
    assert ATTACHING in sess.resumed[0][1]
    assert notifier.sent.count("resumed_for_attach") == 1
