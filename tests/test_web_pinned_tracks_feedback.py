"""Acceptance tests for ticket 04 of pinned-tracks, console side: a parked
pr-open task's feedback pick is its next-launch model, and the run and resume
routes refuse another provider than the feedback pick's. Every task also has
an implement pick on the OTHER provider, so reading the wrong key gives the
wrong answer. Black-box through TestClient."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dispatcher.state import NO_SLOT, PARK_WAKE, Stage
from tests.webfakes import (FakeSources, HEADERS, make_config, make_task,
                            tracks_policy)
from web.app import create_app

FABLE, OPUS, CODEX = "claude-fable-5-1", "claude-opus-5", "openai/gpt-5-codex"
IMPLEMENT = [CODEX, FABLE, OPUS]


def rig(tmp_path, **task_kw):
    fake = FakeSources()
    cfg = replace(make_config(tmp_path),
                  models=tracks_policy(implement=IMPLEMENT))
    fake.tasks_list = [make_task(
        issue=7, stage=Stage.PR_OPEN, slot=NO_SLOT, park=PARK_WAKE,
        pr_number=12, feedback_pending=True,
        picks={"feedback": f"anthropic/{FABLE}@medium",
               "implement": f"{CODEX}@high"}, **task_kw)]
    return fake, TestClient(create_app(cfg, fake))


def test_board_names_the_feedback_pick_as_the_next_launch_model(tmp_path):
    _, client = rig(tmp_path)
    board = client.get("/api/board", headers=HEADERS).json()
    card = next(c for col in board["columns"] for c in col["cards"])
    assert card["model"] == f"anthropic/{FABLE}"


@pytest.mark.parametrize("route", ["resume", "run"])
def test_other_provider_than_the_feedback_pick_is_refused(tmp_path, route):
    fake, client = rig(tmp_path)
    r = client.post(f"/api/task/alpha/7/{route}", headers=HEADERS,
                    json={"model": CODEX})
    assert r.status_code == 422
    assert "runs on anthropic" in r.json()["detail"]
    assert "pick a model from anthropic" in r.json()["detail"]
    assert fake.intents == []


@pytest.mark.parametrize("route", ["resume", "run"])
def test_same_provider_as_the_feedback_pick_is_accepted(tmp_path, route):
    fake, client = rig(tmp_path)
    r = client.post(f"/api/task/alpha/7/{route}", headers=HEADERS,
                    json={"model": f"anthropic/{OPUS}"})
    assert r.status_code == 202
    assert fake.intents[-1][3]["model"] == f"anthropic/{OPUS}"
