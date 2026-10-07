"""Acceptance tests for ticket 03 (pinned tracks): the console shows the pin
and offers the override. Black-box: web.app.create_app through TestClient
(GET /api/board, /api/task/<target>/<issue>, /api/usage, POST .../run)."""
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from dispatcher import priority
from dispatcher.models import parse_policy
from dispatcher.state import PARK_WAKE, Stage
from dispatcher.usage import ProviderUsage, Window, WindowKind
from tests.usagefakes import session_usage
from tests.webfakes import (FakeSources, HEADERS, make_config, make_task,
                            tracks_policy)
from web.app import create_app


def _stages(spec, plan, implement, review):
    return {"when": "x", "spec": spec, "plan": plan, "implement": implement,
            "review": review}


def policy(pinned=("security", "architecture", "frontend")):
    raw = {
        "triage": ["claude-opus-5"], "untracked": "standard",
        "tracks": {
            "standard": _stages(
                ["claude-fable-5-1@medium", "openai/gpt-astra@medium"],
                ["claude-fable-5-1@medium", "openai/gpt-astra@medium"],
                ["claude-sonnet-5@medium", "openai/gpt-sol@medium"],
                ["openai/gpt-astra@medium", "claude-opus-5@medium"]),
            "frontend": _stages(
                ["claude-fable-5-1@medium", "claude-opus-5@medium"],
                ["claude-fable-5-1@medium", "claude-opus-5@medium"],
                ["claude-fable-5-1@medium", "claude-opus-5@medium"],
                ["openai/gpt-astra@medium", "claude-opus-5@medium"]),
            "architecture": _stages(
                ["openai/gpt-astra@high", "claude-fable-5-1@high"],
                ["openai/gpt-astra@high", "claude-fable-5-1@high"],
                ["openai/gpt-astra@medium", "claude-fable-5-1@medium"],
                ["claude-fable-5-1@high", "openai/gpt-astra@high"]),
            "security": _stages(
                ["openai/gpt-astra@high"], ["openai/gpt-astra@high"],
                ["openai/gpt-astra@high"],
                ["claude-fable-5-1@high", "openai/gpt-astra@high"]),
        }}
    if pinned:
        raw["pinned"] = list(pinned)
    return parse_policy(raw)


def anthropic(*denied):
    """Anthropic usage that denies the named model families (scoped week)."""
    now = datetime.now(timezone.utc)
    windows = [Window(WindowKind.SESSION, None, 0.2, now + timedelta(hours=2)),
               Window(WindowKind.WEEKLY, None, 0.0, now + timedelta(days=3.5))]
    windows += [Window(WindowKind.WEEKLY, name, 0.95, now + timedelta(days=3.5))
                for name in denied]
    return ProviderUsage("anthropic", "oauth", time.time(), tuple(windows))


def openai(util=0.2):
    return session_usage(util, provider="openai")


def rig(tmp_path, usages, models=None, mode="openai"):
    fake = FakeSources()
    fake.usages = usages
    cfg = replace(make_config(tmp_path), models=models or policy())
    priority.save(tmp_path, mode, actor="jesdi",
                  now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    return fake, TestClient(create_app(cfg, fake))


def cards(client):
    board = client.get("/api/board", headers=HEADERS).json()
    return {c["issue"]: c for col in board["columns"] for c in col["cards"]}


def detail(client, issue):
    return client.get(f"/api/task/alpha/{issue}", headers=HEADERS).json()


def models(admission):
    return {x["model"]: x["admitted"]
            for x in [admission["requested"], *admission["alternatives"]]}


ALL_ADMITTED = lambda: {"anthropic": anthropic(), "openai": openai()}  # noqa: E731


# --- criterion 1: the board marks a pinned task ----------------------------

def test_board_marks_a_pinned_task_and_leaves_a_standard_task_unpinned(tmp_path):
    fake, client = rig(tmp_path, ALL_ADMITTED())
    fake.tasks_list = [make_task(issue=7, track="frontend"),
                       make_task(issue=8, track="standard")]
    by_issue = cards(client)
    assert by_issue[7]["model"].endswith("claude-fable-5-1")
    assert by_issue[7]["pinned_track"] == "frontend"
    assert by_issue[8]["model"] == "openai/gpt-sol"
    assert by_issue[8]["pinned_track"] == ""


# --- criterion 2: the task detail shows the same pin -----------------------

def test_task_detail_shows_the_same_pin(tmp_path):
    fake, client = rig(tmp_path, ALL_ADMITTED())
    fake.tasks_list = [make_task(issue=7, track="frontend"),
                       make_task(issue=8, track="standard")]
    pinned, plain = detail(client, 7)["card"], detail(client, 8)["card"]
    assert pinned["model"].endswith("claude-fable-5-1")
    assert pinned["pinned_track"] == "frontend"
    assert plain["pinned_track"] == ""


# --- criterion 3: a denied pinned launch says it is pinned -----------------

def denied_frontend(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic("Fable", "Opus"),
                                  "openai": openai()})
    fake.tasks_list = [make_task(issue=7, track="frontend", park=PARK_WAKE)]
    return client


def test_a_denied_pinned_launch_reports_the_pinned_track(tmp_path):
    admission = detail(denied_frontend(tmp_path), 7)["card"]["admission"]
    assert admission["requested"]["model"].endswith("claude-fable-5-1")
    assert admission["requested"]["admitted"] is False
    assert admission["pinned_track"] == "frontend"


# --- criterion 4: the pinned wait offers every model of the policy ---------

def test_a_pinned_wait_offers_every_model_of_the_policy_with_verdicts(tmp_path):
    client = denied_frontend(tmp_path)
    admission = detail(client, 7)["card"]["admission"]
    assert {m.split("/", 1)[-1]: ok for m, ok in models(admission).items()} == {
        "claude-fable-5-1": False, "claude-opus-5": False,
        "claude-sonnet-5": True, "gpt-astra": True, "gpt-sol": True}
    board = cards(client)[7]["admission"]
    assert board == admission


# --- criterion 5: an unpinned wait offers its own list, no pin -------------

def test_an_unpinned_wait_offers_only_its_own_list_and_no_pin(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic("Sonnet"),
                                  "openai": openai(0.95)})
    fake.tasks_list = [make_task(issue=7, track="standard", park=PARK_WAKE)]
    admission = detail(client, 7)["card"]["admission"]
    assert {m.split("/", 1)[-1] for m in models(admission)} == {
        "claude-sonnet-5", "gpt-sol"}
    assert admission["pinned_track"] == ""
    assert cards(client)[7]["pinned_track"] == ""


# --- criterion 6: with a pick only that provider's models are offered ------

def parked_with_pick(tmp_path):
    fake, client = rig(tmp_path, {"anthropic": anthropic("Opus"),
                                  "openai": openai()})
    fake.tasks_list = [make_task(
        issue=7, track="frontend", park=PARK_WAKE,
        picks={"implement": "anthropic/claude-opus-5@medium"})]
    return fake, client


def test_a_pick_limits_a_pinned_wait_to_the_pick_s_provider(tmp_path):
    _, client = parked_with_pick(tmp_path)
    admission = detail(client, 7)["card"]["admission"]
    assert {m for m in models(admission)} == {
        "anthropic/claude-opus-5", "anthropic/claude-fable-5-1",
        "anthropic/claude-sonnet-5"}
    assert admission["pinned_track"] == "frontend"


def test_a_cross_provider_override_is_refused_once_a_pick_exists(tmp_path):
    fake, client = parked_with_pick(tmp_path)
    r = client.post("/api/task/alpha/7/run", headers=HEADERS,
                    json={"model": "openai/gpt-astra"})
    assert r.status_code == 422
    assert "runs on anthropic" in r.json()["detail"]
    assert fake.intents == [] and fake.execution_overrides == {}


# --- criteria 7, 8: /api/usage reports the pinned tracks -------------------

def test_usage_reports_the_pinned_tracks_in_order(tmp_path):
    _, client = rig(tmp_path, ALL_ADMITTED())
    body = client.get("/api/usage", headers=HEADERS).json()
    assert body["priority"]["pinned"] == ["security", "architecture", "frontend"]


def test_usage_reports_no_pinned_track_when_none_is_pinned(tmp_path):
    _, client = rig(tmp_path, ALL_ADMITTED(), models=policy(pinned=()))
    body = client.get("/api/usage", headers=HEADERS).json()
    assert body["priority"]["pinned"] == []


def test_usage_reports_no_pinned_track_for_the_default_policy(tmp_path):
    fake = FakeSources()
    cfg = replace(make_config(tmp_path), models=tracks_policy())
    body = TestClient(create_app(cfg, fake)).get(
        "/api/usage", headers=HEADERS).json()
    assert body["priority"]["pinned"] == []
