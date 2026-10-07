"""A run request with no model arms the model the card names: the first
entry of the next launch's list that the usage gate admits, not the list
head. Black-box through TestClient; policy and usage from
tests/test_web_pinned_tracks.py."""
import pytest

from dispatcher.state import NO_SLOT, Stage
from tests.test_web_pinned_tracks import anthropic, cards, openai, rig
from tests.webfakes import HEADERS, make_task

OPENAI_DENIED = lambda: {"anthropic": anthropic(), "openai": openai(0.99)}  # noqa: E731


@pytest.mark.parametrize("task_kw, armed", [
    # pr-open, no feedback pick: architecture implement = astra, fable.
    (dict(stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=12,
          track="architecture"), "anthropic/claude-fable-5-1"),
    # the same line for a stage that is not pr-open: architecture plan.
    (dict(stage=Stage.PLAN, track="architecture"),
     "anthropic/claude-fable-5-1"),
])
def test_run_with_no_model_arms_the_model_the_card_names(
        tmp_path, task_kw, armed):
    fake, client = rig(tmp_path, OPENAI_DENIED())
    fake.tasks_list = [make_task(issue=7, picks={}, **task_kw)]
    assert cards(client)[7]["model"] == armed
    r = client.post("/api/task/alpha/7/run", headers=HEADERS, json={})
    assert r.status_code == 200
    assert fake.execution_overrides[("alpha", 7)] == (armed, False)


def test_run_with_no_model_arms_the_list_head_when_nothing_is_admitted(
        tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic("fable"),
                                  "openai": openai(0.99)})
    fake.tasks_list = [make_task(issue=7, picks={}, stage=Stage.PLAN,
                                 track="architecture")]
    r = client.post("/api/task/alpha/7/run", headers=HEADERS, json={})
    assert fake.execution_overrides[("alpha", 7)] == ("openai/gpt-astra", False)
