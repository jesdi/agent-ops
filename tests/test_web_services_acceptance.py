"""Acceptance: `GET /api/services` lists the box's recurring jobs in two groups.
Driven through create_app with FakeSources (its `box_timers` are the probe's
raw systemd units); the status report is driven through Probes.timers."""
import io
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.test_box_status_acceptance import NOW, Box
from tests.webfakes import FakeSources, HEADERS, make_config
from web import box_status
from web.app import create_app
from web.box_status import Probes

CLOCK = datetime(2026, 11, 24, 10, 12, tzinfo=timezone.utc)
USER_DIR = "/home/agent/.config/systemd/user"

MANAGED = {  # unit -> (description, OnCalendar)
    "agent-ops-dispatcher": ("Dispatcher pass", "*:0/5"),
    "agent-ops-deploy-sync": ("Deploy sync", "*:0/1"),
    "agent-ops-onboarding": ("Onboarding pass", "*:0/1"),
    "agent-ops-claude-keepalive": ("Claude login keepalive", "*:0/2"),
    "agent-ops-codex-keepalive": ("Codex login keepalive", "*:0/2"),
    "agent-ops-worktree-sweep": ("Stale worktree sweep", "*-*-* 03:00:00"),
    "agent-ops-digest": ("Daily digest", "*-*-* 08:00:00"),
    "agent-ops-triage": ("Backlog triage", "*-*-* 04:00:00"),
}
ROOT = ["secret-cache-refresh", "tracker-deploy-check", "tracker-reminders",
        "tracker-scanner", "tracker-backup"]


def user_timer(name, **kw):
    description, calendar = MANAGED[name]
    return {"scope": "user", "unit": f"{name}.timer",
            "fragment_path": f"{USER_DIR}/{name}.timer", "description": description,
            "active_state": "active", "on_calendar": calendar,
            "last_run": None, "next_run": None, **kw}


def root_timer(name, calendar="*-*-* 00,12:00:00", **kw):
    return {"scope": "system", "unit": f"{name}.timer",
            "fragment_path": f"/etc/systemd/system/{name}.timer",
            "description": name.replace("-", " ").capitalize(),
            "active_state": "active", "on_calendar": calendar,
            "last_run": None, "next_run": None, **kw}


def other_units():
    return [
        {"scope": "user", "unit": "agent-ops-herdr.service",
         "fragment_path": f"{USER_DIR}/agent-ops-herdr.service",
         "description": "herdr", "active_state": "active", "on_calendar": None,
         "last_run": None, "next_run": None},
        {"scope": "system", "unit": "prototypes.service",
         "fragment_path": "/etc/systemd/system/prototypes.service",
         "description": "prototypes", "active_state": "active", "on_calendar": None,
         "last_run": None, "next_run": None},
        {"scope": "system", "unit": "logrotate.timer",
         "fragment_path": "/usr/lib/systemd/system/logrotate.timer",
         "description": "Daily rotation of log files", "active_state": "active",
         "on_calendar": "daily", "last_run": None, "next_run": None},
    ]


def services(tmp_path, timers):
    fake = FakeSources()
    fake.box_timers = timers
    client = TestClient(create_app(make_config(tmp_path), fake, clock=lambda: CLOCK))
    return client.get("/api/services", headers=HEADERS)


def full_box():
    return ([user_timer(n) for n in MANAGED] + [root_timer(n) for n in ROOT]
            + other_units())


def ids(group):
    return [j["id"] for j in group]


def test_lists_eight_managed_and_five_read_only_jobs_and_nothing_else(tmp_path):
    res = services(tmp_path, full_box())

    assert res.status_code == 200
    body = res.json()
    assert sorted(ids(body["managed"])) == sorted(MANAGED)
    assert sorted(ids(body["read_only"])) == sorted(ROOT)
    assert all(not j["read_only"] for j in body["managed"])
    assert all(j["read_only"] for j in body["read_only"])
    listed = set(ids(body["managed"]) + ids(body["read_only"]))
    assert not listed & {"agent-ops-herdr", "prototypes", "logrotate"}


def test_dispatcher_pass_detail_is_active_every_5_min_with_last_and_next_run(tmp_path):
    dispatcher = user_timer(
        "agent-ops-dispatcher",
        last_run={"at": "2026-11-24T10:08:00+00:00", "duration_seconds": 27},
        next_run="2026-11-24T10:13:00+00:00")
    body = services(tmp_path, [dispatcher]).json()

    job = body["managed"][0]
    assert body["now"] == "2026-11-24T10:12:00+00:00"
    assert job["id"] == "agent-ops-dispatcher"
    assert job["state"] == "active"
    assert job["schedule"] == "every 5 min"
    assert job["last_run"] == {"at": "2026-11-24T10:08:00+00:00", "duration_seconds": 27}
    assert job["next_run"] == "2026-11-24T10:13:00+00:00"


def test_a_switched_off_timer_with_no_run_is_paused_never_ran(tmp_path):
    triage = user_timer("agent-ops-triage", active_state="inactive")
    job = services(tmp_path, [triage]).json()["managed"][0]

    assert job["state"] == "paused"
    assert job["last_run"] is None
    assert job["next_run"] is None


def test_a_root_job_is_read_only_with_schedule_and_runs_and_no_actions(tmp_path):
    refresh = root_timer(
        "secret-cache-refresh",
        last_run={"at": "2026-11-24T00:00:00+00:00", "duration_seconds": 3},
        next_run="2026-11-24T12:00:00+00:00")
    body = services(tmp_path, [refresh]).json()

    assert body["managed"] == []
    job = body["read_only"][0]
    assert job["read_only"] is True
    assert job["schedule"]
    assert job["last_run"]["at"] == "2026-11-24T00:00:00+00:00"
    assert job["next_run"] == "2026-11-24T12:00:00+00:00"
    assert not job.get("actions")


def test_status_report_is_unchanged_by_the_services_probe(tmp_path, monkeypatch):
    box = Box(tmp_path, monkeypatch)
    _, before = box.report()

    probes = Probes(units=box.probes().units, tabs=box.probes().tabs,
                    timers=lambda: full_box())
    out = io.StringIO()
    box_status.run(str(box.config), NOW, probes, out)

    assert out.getvalue() == before
