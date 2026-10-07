"""Ticket 03 slices: which pinned track a task's next launch comes from, and
the view fields that carry it (web.app.launch_pinned_track, read_model)."""
from datetime import datetime, timezone

from dispatcher.models import parse_policy
from tests.webfakes import make_task, tracks_policy
from web import read_model
from web.app import launch_pinned_track


def policy(pinned=("frontend",)):
    stages = {s: ["claude-opus-5"] for s in ("spec", "plan", "implement", "review")}
    return parse_policy({
        "triage": ["claude-opus-5"], "untracked": "standard",
        "tracks": {"standard": {"when": "x", **stages},
                   "frontend": {"when": "y", **stages}},
        "pinned": list(pinned)})


def test_the_task_track_is_the_pinned_track_when_it_is_pinned():
    assert launch_pinned_track(make_task(track="frontend"), policy()) == "frontend"


def test_an_unpinned_task_track_gives_no_pinned_track():
    assert launch_pinned_track(make_task(track="standard"), policy()) == ""
    assert launch_pinned_track(make_task(track="frontend"), policy(pinned=())) == ""


def test_a_track_the_policy_does_not_know_gives_no_pinned_track():
    assert launch_pinned_track(make_task(track="legacy"), tracks_policy()) == ""


def test_task_card_carries_the_pinned_track_and_defaults_to_none():
    t = make_task(track="frontend")
    assert read_model.task_card(t, model="m", pinned_track="frontend").pinned_track == "frontend"
    assert read_model.task_card(t, model="m").pinned_track == ""


def test_usage_view_lists_the_pinned_tracks_in_order_and_defaults_to_none():
    kw = dict(now=datetime(2026, 10, 1, tzinfo=timezone.utc),
              pace=read_model.PaceConfig(), default_model="anthropic/claude-opus-5",
              mode="auto", routed=["anthropic"])
    assert read_model.usage_view({}, pinned=("b", "a"), **kw).priority.pinned == ["b", "a"]
    assert read_model.usage_view({}, **kw).priority.pinned == []
