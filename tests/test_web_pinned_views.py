"""Ticket 03 slices: which pinned track a task's next launch comes from, and
the view fields that carry it (web.app.launch_pinned_track, read_model)."""
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from dispatcher.models import parse_policy
from dispatcher.state import PARK_WAKE, Stage
from tests.pinned import anthropic, openai, rig, web_policy
from tests.webfakes import FakeSources, HEADERS, make_config, make_task, tracks_policy
from web import read_model
from web.app import create_app, launch_pinned_track


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


def test_a_task_with_no_next_launch_gives_no_pinned_track():
    for stage in (Stage.DONE, Stage.FAILED, Stage.CANCELED):
        assert launch_pinned_track(make_task(track="frontend", stage=stage), policy()) == ""


def test_a_one_shot_override_unpins_the_next_launch_but_a_pick_does_not():
    t = make_task(track="frontend")
    assert launch_pinned_track(t, policy(), overridden=True) == ""
    assert launch_pinned_track(
        make_task(track="frontend", picks={"implement": "anthropic/claude-opus-5@medium"}),
        policy()) == "frontend"


def client(tmp_path, tasks):
    fake = FakeSources()
    fake.tasks_list = tasks
    cfg = replace(make_config(tmp_path), models=policy())
    return fake, TestClient(create_app(cfg, fake))


def by_issue(body):
    return {c["issue"]: c for col in body["columns"] for c in col["cards"]}


def test_the_snapshot_marks_a_pinned_card_and_leaves_an_unpinned_one_empty(tmp_path):
    _, c = client(tmp_path, [make_task(issue=7, track="frontend"),
                             make_task(issue=8, track="standard")])
    cards = by_issue(c.get("/api/board/snapshot", headers=HEADERS).json())
    assert cards[7]["pinned_track"] == "frontend"
    assert cards[8]["pinned_track"] == ""


def test_a_done_task_and_an_overridden_task_carry_no_pin_on_the_board(tmp_path):
    fake, c = client(tmp_path, [
        make_task(issue=7, track="frontend", stage=Stage.DONE),
        make_task(issue=8, track="frontend", park=PARK_WAKE,
                  resume_model_override="openai/gpt-astra"),
        make_task(issue=9, track="frontend")])
    fake.set_execution_override("alpha", 9, model="openai/gpt-astra", bypass_usage=False)
    for route in ("/api/board", "/api/board/snapshot"):
        cards = by_issue(c.get(route, headers=HEADERS).json())
        assert [cards[i]["pinned_track"] for i in (7, 8, 9)] == ["", "", ""], route
    assert c.get("/api/task/alpha/9", headers=HEADERS).json()["card"]["pinned_track"] == ""


def test_a_task_with_no_track_shows_the_untracked_tracks_model_and_pin(tmp_path):
    """A task claimed before tracks existed runs as untracked work. The
    board names the model the dispatcher launches for it, and the pin: here
    the untracked track is pinned, so the written-first entry, not the one
    mode `openai` would put first."""
    fake, c = rig(tmp_path, {"anthropic": anthropic(), "openai": openai()},
                  models=web_policy(pinned=("standard",)))
    fake.tasks_list = [make_task(issue=7, track="", park=PARK_WAKE)]
    for route in ("/api/board", "/api/board/snapshot"):
        card = by_issue(c.get(route, headers=HEADERS).json())[7]
        assert card["model"] == "anthropic/claude-sonnet-5", route
        assert card["pinned_track"] == "standard", route


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
