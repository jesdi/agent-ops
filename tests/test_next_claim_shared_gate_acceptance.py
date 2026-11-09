"""Acceptance: the console's claim forecast works out its local blockers through
one shared gate (auth-dark verdict, quarantine skipping, existing order kept).
Driven through web.read_model.next_claim and GET /api/board only."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from dispatcher.state import PARK_HUMAN, PARK_LOGIN, Stage
from tests.configfakes import make_project
from tests.usagefakes import session_usage
from tests.webfakes import FakeSources, HEADERS, make_config, make_task
from web.app import create_app
from web.read_model import next_claim, usage_view
from dispatcher.usage import PaceConfig, unavailable

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
PACE = PaceConfig(0.8, 30, 0.95, 0.10, 1.0, "UTC")
DEFAULT = "claude-opus-4-8"
FRESH = {"finished_at": "2026-11-06T11:57:00+00:00", "interval_minutes": 5}
PROJECTS = {"alpha": make_project("alpha"), "beta": make_project("beta")}


def gate_for(usages):
    return usage_view(usages, now=NOW, pace=PACE, default_model=DEFAULT,
                      mode="auto", routed={"anthropic"}).gate


GATE_OK = gate_for({"anthropic": session_usage(0.5, now=NOW)})
GATE_BUDGET = gate_for({"anthropic": session_usage(0.95, 40, now=NOW)})
GATE_DARK = gate_for({"anthropic": unavailable("anthropic", NOW.timestamp())})


def rows(repository, *numbers):
    return (repository, [{"repository": repository, "number": n,
                          "status": "Ready", "blocked": False, "labels": ["auto"],
                          "title": f"t{n}", "url": "u", "boost": 0, "score": 1.0}
                         for n in numbers])


def forecast(heartbeat=FRESH, tasks=(), capacity=3, gate=GATE_OK,
             queues=None, claims_paused=False, projects=PROJECTS):
    return next_claim(heartbeat, now=NOW, tasks=list(tasks), capacity=capacity,
                      gate=gate, queues=queues or [rows("alpha", 50)],
                      claims_paused=claims_paused, projects=projects,
                      last_claimed={}).verdict


def test_dead_login_is_auth_dark_not_budget_blocked():
    assert GATE_DARK.admitted is False
    assert forecast(gate=GATE_DARK) == "auth-dark"


def test_board_route_reports_auth_dark_with_non_empty_queue(tmp_path):
    fake = FakeSources()
    fake.usages = {"anthropic": unavailable("anthropic", NOW.timestamp())}
    now = datetime.now(timezone.utc)
    fake.heartbeat = {"finished_at": (now - timedelta(minutes=1)).isoformat(),
                      "interval_minutes": 10}
    fake.rank["alpha"] = ([{"number": 73, "title": "t", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}],
                          "2026-08-01T12:00:00+00:00", False)
    client = TestClient(create_app(make_config(tmp_path), fake))
    body = client.get("/api/board", headers=HEADERS).json()
    assert body["next_claim"]["verdict"] == "auth-dark"


def test_quarantined_queue_head_is_skipped_for_the_next_candidate():
    v = next_claim(FRESH, now=NOW, tasks=[], capacity=3, gate=GATE_OK,
                   queues=[rows("alpha", 212, 213)], projects=PROJECTS,
                   last_claimed={}, quarantined={("alpha", 212)})
    assert (v.verdict, v.next_repository, v.next_issue) == (
        "will-claim", "alpha", 213)


def test_board_route_skips_a_quarantined_head(tmp_path):
    fake = FakeSources()
    now = datetime.now(timezone.utc)
    fake.heartbeat = {"finished_at": (now - timedelta(minutes=1)).isoformat(),
                      "interval_minutes": 10}
    fake.rank["alpha"] = ([{"number": n, "title": "t", "url": "u",
                            "status": "Ready", "labels": ["auto"],
                            "blocked": False, "score": 2.0, "boost": 0}
                           for n in (212, 213)], "2026-08-01T12:00:00+00:00", False)
    fake.quarantine = [{"repository": "alpha", "task_issue": 212,
                        "blocker_repo": "alpha", "blocker_issue": 205,
                        "fingerprint": "", "created_at": ""}]
    client = TestClient(create_app(make_config(tmp_path), fake))
    nc = client.get("/api/board", headers=HEADERS).json()["next_claim"]
    assert (nc["verdict"], nc["next_issue"]) == ("will-claim", 213)


def test_quarantine_of_beta_73_does_not_hide_alpha_73():
    v = next_claim(FRESH, now=NOW, tasks=[], capacity=3, gate=GATE_OK,
                   queues=[rows("alpha", 73)], projects=PROJECTS,
                   last_claimed={}, quarantined={("beta", 73)})
    assert (v.verdict, v.next_repository, v.next_issue) == (
        "will-claim", "alpha", 73)


def _inflight(n, **kw):
    return [make_task(issue=i, **kw) for i in range(1, n + 1)]


def test_console_verdict_matches_the_commands_blocker_for_every_local_state():
    stale = dict(FRESH, finished_at="2026-11-06T11:49:00+00:00")
    edge = dict(FRESH, finished_at="2026-11-06T11:50:00+00:00")
    naive = dict(FRESH, finished_at="2026-11-06T11:57:00")
    paused = {n: make_project(n, paused=True) for n in ("alpha", "beta")}
    one_paused = {"alpha": make_project("alpha"),
                  "beta": make_project("beta", paused=True)}
    parked = _inflight(2, park=PARK_HUMAN) + [make_task(issue=9)]
    login = _inflight(2) + [make_task(issue=9, park=PARK_LOGIN)]
    cases = {
        "stale": (forecast(stale), "unknown"),
        "missing": (forecast(None), "unknown"),
        "naive": (forecast(naive), "unknown"),
        "edge": (forecast(edge), "will-claim"),
        "triage over budget and paused": (
            forecast(gate=GATE_BUDGET, claims_paused=True, projects=paused),
            "claims-paused"),
        "triage over auth-dark": (
            forecast(gate=GATE_DARK, claims_paused=True), "claims-paused"),
        "budget": (forecast(gate=GATE_BUDGET), "budget-blocked"),
        "auth-dark over paused and full": (
            forecast(gate=GATE_DARK, projects=paused, tasks=_inflight(3)),
            "auth-dark"),
        "budget over paused": (
            forecast(gate=GATE_BUDGET, projects=paused), "budget-blocked"),
        "all paused": (forecast(projects=paused), "projects-paused"),
        "one paused": (forecast(projects=one_paused), "will-claim"),
        "full": (forecast(tasks=_inflight(3)), "capacity-full"),
        "parked free slots": (forecast(tasks=parked), "will-claim"),
        "parked-login keeps slot": (forecast(tasks=login), "capacity-full"),
    }
    wrong = {k: got for k, (got, want) in cases.items() if got != want}
    assert wrong == {}
