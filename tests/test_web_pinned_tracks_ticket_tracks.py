"""Acceptance tests for ticket 06 of pinned-tracks, console side: a task that
waits for a ticket with a ticket track shows that track as the pin and offers
the override list. The state is given directly (ticket_tracks maps ticket
number to track name, int keys). Black-box through GET /api/board and
/api/task/<target>/<issue>."""
from dispatcher.state import Stage
from tests.test_web_pinned_tracks import (anthropic, cards, detail, models,
                                          openai, rig)
from tests.webfakes import make_task


def waiting(tmp_path, cursor, usages):
    fake, client = rig(tmp_path, usages)
    fake.tasks_list = [make_task(
        issue=7, track="architecture", stage=Stage.IMPLEMENT,
        ticket_cursor=cursor, ticket_count=3, ticket_tracks={2: "frontend"})]
    return client


def test_a_waiting_ticket_track_is_the_next_launch_and_offers_an_override(
        tmp_path):
    client = waiting(tmp_path, 1, {"anthropic": anthropic("Fable", "Opus"),
                                   "openai": openai()})
    card = cards(client)[7]
    assert card["model"].endswith("claude-fable-5-1")
    assert card["pinned_track"] == "frontend"
    admission = detail(client, 7)["card"]["admission"]
    assert admission["requested"]["model"].endswith("claude-fable-5-1")
    assert admission["pinned_track"] == "frontend"
    offered = models(admission)
    assert offered["openai/gpt-astra"] is True
    assert any(m.startswith("openai/") for m in offered)
    assert card["admission"] == admission


def test_the_next_ticket_without_a_track_shows_the_task_track_pin(tmp_path):
    client = waiting(tmp_path, 2, {"anthropic": anthropic("Fable", "Opus"),
                                   "openai": openai()})
    card = cards(client)[7]
    assert card["model"] == "openai/gpt-astra"
    assert card["pinned_track"] == "architecture"
