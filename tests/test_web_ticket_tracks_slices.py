"""Console slices for ticket 06 of pinned-tracks: the pinned track of a
ticket launch (web.app.launch_pinned_track), the limit a ticket's pick sets
on the override list, and a ticket track that is no longer pinned."""
from dispatcher.state import PARK_HUMAN, Stage
from tests.test_web_pinned_tracks import (HEADERS, anthropic, cards, detail,
                                          models, openai, policy, rig)
from tests.webfakes import make_task
from web.app import launch_pinned_track

OPUS_PICK = {"implement": "anthropic/claude-opus-5@medium"}


def ticket_task(**kw):
    defaults = dict(issue=7, track="standard", stage=Stage.IMPLEMENT,
                    ticket_cursor=1, ticket_count=3,
                    ticket_tracks={2: "frontend"})
    return make_task(**{**defaults, **kw})


def test_the_next_tickets_track_is_the_pinned_track_of_an_unpinned_task():
    assert launch_pinned_track(ticket_task(), policy()) == "frontend"


def test_a_ticket_in_progress_keeps_its_own_pin():
    assert launch_pinned_track(ticket_task(picks=OPUS_PICK), policy()) == ""
    assert launch_pinned_track(
        ticket_task(picks=OPUS_PICK, ticket_cursor=2), policy()) == "frontend"


def test_pr_feedback_of_a_task_with_ticket_tracks_shows_the_task_track():
    t = ticket_task(stage=Stage.PR_OPEN, track="architecture", ticket_cursor=1)
    assert launch_pinned_track(t, policy()) == "architecture"


def test_a_ticket_track_no_longer_pinned_names_no_pin_and_no_model(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic(), "openai": openai()},
                       models=policy(pinned=("security", "architecture")))
    fake.tasks_list = [ticket_task(park=PARK_HUMAN)]
    card = cards(client)[7]
    assert card["pinned_track"] == "" and card["model"] == ""


def test_a_tickets_pick_limits_the_override_to_its_provider(tmp_path):
    """Spec scenario "with a pick, only that provider's models are offered"."""
    fake, client = rig(tmp_path, {"anthropic": anthropic("Opus"),
                                  "openai": openai()})
    fake.tasks_list = [ticket_task(track="architecture", ticket_cursor=2,
                                   picks=OPUS_PICK, park=PARK_HUMAN)]
    admission = detail(client, 7)["card"]["admission"]
    assert admission["pinned_track"] == "frontend"
    offered = models(admission)
    assert {"anthropic/claude-fable-5-1", "anthropic/claude-sonnet-5"} <= set(offered)
    assert not any(m.startswith("openai/") for m in offered)
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "openai/gpt-astra"})
    assert r.status_code == 422


def test_between_tickets_a_one_shot_override_may_name_any_provider(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic("Fable", "Opus"),
                                  "openai": openai()})
    fake.tasks_list = [ticket_task(track="architecture")]
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "openai/gpt-astra"})
    assert r.status_code == 200
    assert fake.execution_overrides[("alpha", 7)] == ("openai/gpt-astra", False)
