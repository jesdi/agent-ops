"""Acceptance: `POST /api/services/{job}/run` and `/stop`. Driven through
create_app with FakeSources, whose `start_unit` / `stop_unit` are the fake
`systemctl` (`systemctl_calls` records each call) and whose `box_runs` and
`box_timers` are what the box then reports."""
import pytest
from fastapi.testclient import TestClient

from tests.test_web_services_acceptance import CLOCK, full_box, user_timer
from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


def box(tmp_path, timers, runs=None):
    fake = FakeSources()
    fake.box_timers = timers
    fake.box_runs = runs or {}
    fake.box_now = CLOCK.isoformat()
    client = TestClient(create_app(make_config(tmp_path), fake, clock=lambda: CLOCK))
    return fake, client


def job(client, job_id):
    body = client.get("/api/services", headers=HEADERS).json()
    return next(j for g in ("managed", "read_only") for j in body[g] if j["id"] == job_id)


def post(client, job_id, verb):
    return client.post(f"/api/services/{job_id}/{verb}", headers=HEADERS)


def test_run_now_on_an_active_job_runs_it_and_leaves_its_next_run(tmp_path):
    sweep = user_timer("agent-ops-worktree-sweep", next_run="2026-11-25T03:00:00+00:00")
    fake, client = box(tmp_path, [sweep])

    assert post(client, "agent-ops-worktree-sweep", "run").status_code == 200

    assert fake.systemctl_calls == [("start", "agent-ops-worktree-sweep.service")]
    assert job(client, "agent-ops-worktree-sweep")["state"] == "running"
    fake.finish_runs()
    after = job(client, "agent-ops-worktree-sweep")
    assert after["state"] == "active"
    assert after["last_run"]["at"] == "2026-11-24T10:12:00+00:00"
    assert after["next_run"] == "2026-11-25T03:00:00+00:00"


def test_run_now_on_a_paused_job_runs_it_once_and_it_stays_paused(tmp_path):
    triage = user_timer("agent-ops-triage", active_state="inactive")
    fake, client = box(tmp_path, [triage])

    assert post(client, "agent-ops-triage", "run").status_code == 200

    assert fake.systemctl_calls == [("start", "agent-ops-triage.service")]
    fake.finish_runs()
    after = job(client, "agent-ops-triage")
    assert after["state"] == "paused"
    assert after["last_run"]["at"] == "2026-11-24T10:12:00+00:00"


def test_stop_run_ends_the_run_as_stopped_and_the_job_stays_active(tmp_path):
    dispatcher = user_timer("agent-ops-dispatcher", next_run="2026-11-24T10:13:00+00:00")
    earlier = {"at": "2026-11-24T10:05:00+00:00", "duration_seconds": 20, "result": "success"}
    going = {"at": "2026-11-24T10:10:00+00:00", "duration_seconds": None, "result": None}
    fake, client = box(tmp_path, [dispatcher], {"agent-ops-dispatcher": [earlier, going]})

    res = post(client, "agent-ops-dispatcher", "stop")

    assert res.status_code == 200
    assert fake.systemctl_calls == [("stop", "agent-ops-dispatcher.service")]
    after = job(client, "agent-ops-dispatcher")
    assert after["recent_runs"][0]["at"] == "2026-11-24T10:10:00+00:00"
    assert after["recent_runs"][0]["result"] == "stopped"
    assert after["state"] == "active"
    assert after["next_run"] == "2026-11-24T10:13:00+00:00"


@pytest.mark.parametrize("job_id,verb", [
    ("secret-cache-refresh", "run"),
    ("secret-cache-refresh", "stop"),
    ("tracker-backup", "run"),
    ("agent-ops-herdr", "stop"),
    ("agent-ops-herdr", "run"),
    ("agent-ops-herdr.service", "stop"),
    ("agent-ops-dispatcher.service", "run"),
    ("agent-ops-dispatcher.timer", "stop"),
    ("prototypes", "run"),
    ("logrotate", "run"),
    ("no-such-job", "run"),
])
def test_a_job_that_is_not_managed_is_refused_without_calling_systemctl(
        tmp_path, job_id, verb):
    fake, client = box(tmp_path, full_box())

    res = post(client, job_id, verb)

    assert res.status_code in (400, 403, 404, 422)
    assert fake.systemctl_calls == []
