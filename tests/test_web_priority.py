"""Acceptance tests for ticket 04 (provider priority): the console sets and
reports the priority mode. Black-box: web.app.create_app through TestClient
(POST /api/priority, GET /api/usage, /api/board, /api/board/snapshot), the
stored mode through dispatcher.priority, events through the sources' event
sink. Usage windows are built from one frozen NOW that is also frozen inside
the app, so required pace and allowances are exact."""
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dispatcher import priority
from dispatcher.state import Stage
from dispatcher.usage import PaceConfig, ProviderUsage, Window, WindowKind
from tests.webfakes import (FakeSources, make_config, make_task,
                            tracks_policy)
from web.app import create_app

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
OP = {"Tailscale-User-Login": "jesdi"}
SONNET, LUNA = "claude-sonnet-5-5", "openai/gpt-6-luna@high"
LUNA_BOARD = "openai/gpt-6-luna"   # board names model_id: effort is dropped
PACE = PaceConfig(weekend_weight=1.0, pace_margin=1.0)
S, W_ = WindowKind.SESSION, WindowKind.WEEKLY


def freeze(monkeypatch, now=NOW):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    for mod in ("web.app", "web.read_model"):
        monkeypatch.setattr(f"{mod}.datetime", Frozen, raising=False)


def wk(used, hours, scope=None):
    return Window(W_, scope, used, NOW + timedelta(hours=hours))


def ses(used, hours=2.0):
    return Window(S, None, used, NOW + timedelta(hours=hours))


def usage(provider, *windows, source="oauth"):
    return ProviderUsage(provider, source, time.time(), tuple(windows))


class Rec(FakeSources):
    """FakeSources that records the stored mode at the moment an event is
    appended and counts usage fetches."""

    def __init__(self, state_dir):
        super().__init__()
        self.mode_at_append = []
        self.usage_calls = 0
        self._dir = state_dir

    def append_event(self, event, **kw):
        self.mode_at_append.append(priority.load(self._dir, {"anthropic", "openai"}))
        super().append_event(event, **kw)

    def usage(self):
        self.usage_calls += 1
        return super().usage()


def rig(tmp_path, monkeypatch=None, usages=None, pace=PACE, routed_openai=True):
    if monkeypatch:
        freeze(monkeypatch)
    fake = Rec(tmp_path)
    if usages is not None:
        fake.usages = usages
    cfg = make_config(tmp_path)
    models = tracks_policy(**{s: [SONNET, LUNA] if routed_openai else [SONNET]
                              for s in ("spec", "plan", "implement", "review")})
    cfg = replace(cfg, models=models, pace=pace)
    return fake, cfg, TestClient(create_app(cfg, fake))


def get_usage(client):
    return client.get("/api/usage", headers=OP).json()


def stored(tmp_path):
    return priority.load(tmp_path, {"anthropic", "openai"})


def paces(body):
    return {p["provider"]: [(w["kind"], w["scope"], w["required_pace"])
                            for w in p["windows"]] for p in body["providers"]}


def both(a, o):
    return {"anthropic": usage("anthropic", *a), "openai": usage("openai", *o)}


# --- reporting the mode ----------------------------------------------------

def test_no_stored_mode_reports_auto_and_the_options(tmp_path):
    _, _, client = rig(tmp_path)
    p = get_usage(client)["priority"]
    assert p["mode"] == "auto"
    assert p["options"] == ["auto", "anthropic", "openai"]


def test_truncated_mode_file_reports_auto(tmp_path):
    (tmp_path / priority.FILE).write_text('{"mode": "ope')
    _, _, client = rig(tmp_path)
    assert get_usage(client)["priority"]["mode"] == "auto"


def test_unrouted_provider_in_the_file_reports_auto(tmp_path):
    priority.save(tmp_path, "nvidia", actor="jesdi", now=NOW)
    _, _, client = rig(tmp_path)
    assert get_usage(client)["priority"]["mode"] == "auto"


# --- setting it ------------------------------------------------------------

def test_operator_sets_the_mode_stores_it_and_records_one_event(tmp_path):
    fake, _, client = rig(tmp_path)
    r = client.post("/api/priority", headers=OP, json={"mode": "openai"})
    assert r.status_code == 200
    assert get_usage(client)["priority"]["mode"] == "openai"
    assert stored(tmp_path) == "openai"
    assert [(e, a, d) for e, _t, _i, a, d in fake.appended] == [
        ("priority-mode-set", "jesdi", "mode=openai")]
    assert fake.mode_at_append == ["openai"], "event is written after the file"


def test_the_mode_survives_a_console_restart(tmp_path):
    _, cfg, client = rig(tmp_path)
    assert client.post("/api/priority", headers=OP,
                       json={"mode": "openai"}).status_code == 200
    again = TestClient(create_app(cfg, FakeSources()))
    assert get_usage(again)["priority"]["mode"] == "openai"


def test_unknown_mode_is_422_and_changes_nothing(tmp_path):
    fake, _, client = rig(tmp_path)
    r = client.post("/api/priority", headers=OP, json={"mode": "nvidia"})
    assert r.status_code == 422
    assert stored(tmp_path) == "auto"
    assert get_usage(client)["priority"]["mode"] == "auto"
    assert fake.appended == []


def test_no_operator_session_is_refused_like_any_console_write(tmp_path):
    fake, _, client = rig(tmp_path)
    r = client.post("/api/priority", json={"mode": "openai"})
    assert r.status_code == 401   # as test_web_auth: missing header is 401
    assert stored(tmp_path) == "auto"
    assert fake.appended == []


# --- which provider is first, and the required pace ------------------------

def test_auto_marks_the_higher_required_pace_first_and_reports_paces(
        tmp_path, monkeypatch):
    u = both([ses(0.5), wk(0.10, 96)], [ses(0.5), wk(0.20, 24)])
    _, _, client = rig(tmp_path, monkeypatch, u)
    body = get_usage(client)
    assert body["priority"]["first"] == "openai"
    assert paces(body) == {
        "anthropic": [("session", None, None), ("weekly", None, 1.6)],
        "openai": [("session", None, None), ("weekly", None, 5.6)]}


def test_auto_equal_required_pace_marks_anthropic_first(tmp_path, monkeypatch):
    u = both([wk(0.5, 84)], [wk(0.5, 84)])
    _, _, client = rig(tmp_path, monkeypatch, u)
    body = get_usage(client)
    assert body["priority"]["first"] == "anthropic"
    assert [w[2] for ws in paces(body).values() for w in ws] == [1.0, 1.0]


def test_auto_with_no_usage_available_marks_anthropic_first(
        tmp_path, monkeypatch):
    u = {"anthropic": usage("anthropic", source="unavailable"),
         "openai": usage("openai", source="unavailable")}
    _, _, client = rig(tmp_path, monkeypatch, u)
    assert get_usage(client)["priority"]["first"] == "anthropic"


def test_auto_session_bound_anthropic_is_first_despite_lower_pace(
        tmp_path, monkeypatch):
    u = both([ses(0.0, 5), wk(0.70, 30)], [wk(0.20, 24)])
    pace = PaceConfig(weekend_weight=1.0, pace_margin=1.0,
                      session_week_share={"anthropic": 0.05})
    _, _, client = rig(tmp_path, monkeypatch, u, pace=pace)
    body = get_usage(client)
    assert body["priority"]["first"] == "anthropic"
    assert paces(body)["anthropic"][1][2] == 1.7
    assert paces(body)["openai"][0][2] == 5.6


def test_provider_first_mode_marks_that_provider_first(tmp_path, monkeypatch):
    u = both([wk(0.10, 96)], [wk(0.20, 24)])
    _, _, client = rig(tmp_path, monkeypatch, u)
    priority.save(tmp_path, "anthropic", actor="jesdi", now=NOW)
    body = get_usage(client)
    assert body["priority"]["mode"] == "anthropic"
    assert body["priority"]["first"] == "anthropic"


# --- the gate's numbers do not change with the mode ------------------------

def test_gate_numbers_are_identical_under_auto_and_openai(
        tmp_path, monkeypatch):
    pace = PaceConfig(weekend_weight=1.0, pace_margin=0.1)
    u = {"anthropic": usage("anthropic", ses(0.5, 2.0), wk(0.60, 60))}
    _, _, client = rig(tmp_path, monkeypatch, u, pace=pace)

    def fields(body):
        (p,) = body["providers"]
        return {w["kind"]: (w["allowance"], w["headroom"], w["severity"])
                for w in p["windows"]}
    auto = fields(get_usage(client))
    assert client.post("/api/priority", headers=OP,
                       json={"mode": "openai"}).status_code == 200
    openai = fields(get_usage(client))
    assert auto == openai
    s, w = auto["session"], auto["weekly"]
    assert (s[0], s[1], s[2]) == (pytest.approx(0.8), pytest.approx(0.3), "ok")
    assert (w[0], w[1], w[2]) == (pytest.approx(0.742857, abs=1e-5),
                                  pytest.approx(0.142857, abs=1e-5), "ok")


# --- the board names the launch the mode picks -----------------------------

def board_model(client):
    cards = [c for col in client.get("/api/board", headers=OP).json()["columns"]
             for c in col["cards"]]
    return cards[0]["model"]


def with_task(fake):
    fake.tasks_list = [make_task(issue=7, stage=Stage.PLAN)]


def admitted_usage():
    return both([ses(0.1), wk(0.10, 96)], [ses(0.1), wk(0.20, 24)])


def test_board_names_the_openai_entry_under_mode_openai(tmp_path, monkeypatch):
    fake, _, client = rig(tmp_path, monkeypatch, both(
        [ses(0.1), wk(0.50, 84)], [ses(0.1), wk(0.50, 84)]))
    with_task(fake)
    priority.save(tmp_path, "openai", actor="jesdi", now=NOW)
    assert board_model(client).split("@")[0] == LUNA_BOARD


def test_board_names_the_openai_entry_when_auto_ranks_it_first(
        tmp_path, monkeypatch):
    fake, _, client = rig(tmp_path, monkeypatch, admitted_usage())
    with_task(fake)
    assert board_model(client).split("@")[0] == LUNA_BOARD


def test_snapshot_names_the_openai_entry_under_mode_openai_without_usage(
        tmp_path, monkeypatch):
    fake, _, client = rig(tmp_path, monkeypatch, admitted_usage())
    with_task(fake)
    priority.save(tmp_path, "openai", actor="jesdi", now=NOW)
    body = client.get("/api/board/snapshot", headers=OP).json()
    (card,) = [c for col in body["columns"] for c in col["cards"]]
    assert card["model"].split("@")[0] == LUNA_BOARD
    assert fake.usage_calls == 0
