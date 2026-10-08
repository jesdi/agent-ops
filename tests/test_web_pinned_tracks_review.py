"""Acceptance tests for ticket 05 of pinned-tracks, board side: the card of a
task about to enter review (stage review, no review pick yet) shows the model
the dispatcher will launch. Same policy and rule as
tests/test_pinned_tracks_review_avoid.py; the recorded providers are given in
the task state."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from dispatcher import priority
from dispatcher.state import Stage
from tests.test_pinned_tracks_order import (ASTRA, FABLE, NOW, OPUS, PACE, SOL,
                                            SONNET, ahead, policy)
from tests.test_web_priority import freeze
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app

BOTH = ["openai", "anthropic"]


def card_model(tmp_path, monkeypatch, track, mode, implement_pick, providers):
    freeze(monkeypatch)
    fake = FakeSources()
    fake.usages = ahead()
    cfg = replace(make_config(tmp_path), models=policy(), pace=PACE)
    if mode:
        priority.save(str(tmp_path), mode, actor="op", now=NOW)
    fake.tasks_list = [make_task(
        issue=7, stage=Stage.REVIEW, track=track,
        picks={"implement": implement_pick},
        implement_providers=list(providers))]
    board = TestClient(create_app(cfg, fake)).get(
        "/api/board", headers=HEADERS).json()
    return next(c for col in board["columns"] for c in col["cards"])["model"]


CASES = [
    # (track, mode, implement pick, recorded providers, next review model)
    ("standard", "openai", f"{SOL}@medium", ["openai"], f"anthropic/{OPUS}"),
    ("architecture", None, f"anthropic/{FABLE}@medium", BOTH,
     f"anthropic/{FABLE}"),
    ("standard", "anthropic", f"anthropic/{SONNET}@medium", BOTH,
     f"anthropic/{OPUS}"),
    ("standard", "openai", f"{SOL}@medium", BOTH, ASTRA),
    ("frontend", None, f"anthropic/{OPUS}@medium", ["anthropic"], ASTRA),
]


@pytest.mark.parametrize("track,mode,pick,providers,expected", CASES,
                         ids=["one-provider", "architecture-both",
                              "standard-both-anthropic",
                              "standard-both-openai",
                              "two-models-one-provider"])
def test_board_review_model_follows_the_review_avoid_rule(
        tmp_path, monkeypatch, track, mode, pick, providers, expected):
    assert card_model(tmp_path, monkeypatch, track, mode, pick,
                      providers) == expected
