"""Acceptance: `GET /api/services` detail carries a managed job's five newest runs
(newest first) and its next three run times. Driven through create_app with
FakeSources: `box_timers` are the timer probe, `box_runs` the journal probe."""
from fastapi.testclient import TestClient

from tests.test_web_services_acceptance import (
    CLOCK, root_timer, services, user_timer)
from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


def run(minute, seconds=20, result="success"):
    return {"at": f"2026-11-24T09:{minute:02d}:00+00:00",
            "duration_seconds": seconds, "result": result}


def job_of(tmp_path, timers, runs, group="managed"):
    fake = FakeSources()
    fake.box_timers = timers
    fake.box_runs = runs
    client = TestClient(create_app(make_config(tmp_path), fake, clock=lambda: CLOCK))
    return client.get("/api/services", headers=HEADERS).json()[group][0]


def test_detail_lists_exactly_the_five_newest_runs_newest_first(tmp_path):
    journal = [run(m, seconds=m, result="failed" if m == 50 else "success")
               for m in range(0, 60, 5)]  # 12 runs, oldest first
    timer = user_timer("agent-ops-dispatcher", next_run="2026-11-24T10:13:00+00:00")

    job = job_of(tmp_path, [timer], {"agent-ops-dispatcher": journal})

    assert [r["at"] for r in job["recent_runs"]] == [
        f"2026-11-24T09:{m:02d}:00+00:00" for m in (55, 50, 45, 40, 35)]
    assert [r["result"] for r in job["recent_runs"]] == [
        "success", "failed", "success", "success", "success"]
    assert [r["duration_seconds"] for r in job["recent_runs"]] == [55, 50, 45, 40, 35]


def test_an_unfinished_run_is_listed_as_running_without_a_duration(tmp_path):
    journal = [run(0), {"at": "2026-11-24T10:10:00+00:00",
                        "duration_seconds": None, "result": None}]
    timer = user_timer("agent-ops-dispatcher", next_run="2026-11-24T10:13:00+00:00")

    job = job_of(tmp_path, [timer], {"agent-ops-dispatcher": journal})

    assert job["recent_runs"][0]["at"] == "2026-11-24T10:10:00+00:00"
    assert job["recent_runs"][0]["duration_seconds"] is None


def test_a_five_minute_job_lists_the_next_three_run_times(tmp_path):
    timer = user_timer("agent-ops-dispatcher", next_run="2026-11-24T10:13:00+00:00")

    job = job_of(tmp_path, [timer], {})

    assert job["upcoming"] == ["2026-11-24T10:13:00+00:00",
                               "2026-11-24T10:18:00+00:00",
                               "2026-11-24T10:23:00+00:00"]


def test_a_daily_job_lists_its_03_00_run_among_the_upcoming(tmp_path):
    timer = user_timer("agent-ops-worktree-sweep", next_run="2026-11-25T03:00:00+00:00")

    job = job_of(tmp_path, [timer], {})

    assert job["upcoming"][0] == "2026-11-25T03:00:00+00:00"


def test_a_job_with_no_journal_entries_has_no_recent_runs(tmp_path):
    timer = user_timer("agent-ops-triage", active_state="inactive")

    job = job_of(tmp_path, [timer], {})

    assert job["recent_runs"] == []


def test_a_paused_job_has_no_upcoming_runs(tmp_path):
    timer = user_timer("agent-ops-triage", active_state="inactive")

    job = job_of(tmp_path, [timer], {})

    assert job["state"] == "paused"
    assert job["upcoming"] == []


def test_a_read_only_job_lists_upcoming_runs_but_no_recent_runs(tmp_path):
    timer = root_timer("secret-cache-refresh", next_run="2026-11-25T00:00:00+00:00")

    job = job_of(tmp_path, [timer], {"secret-cache-refresh": [run(0)]}, "read_only")

    assert job["upcoming"][0] == "2026-11-25T00:00:00+00:00"
    assert not job.get("recent_runs")
