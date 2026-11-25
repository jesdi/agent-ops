"""Acceptance (services-section ticket 10): changing a managed job's schedule from the console.

Black-box: `PUT /api/services/{job}/schedule` and `GET /api/services` through TestClient, the
recorder through `dispatcher.services_record.run(box, sh=, get=)` with the fake infra repo.
Interface defined here (the ticket leaves it open):
- the body of the PUT is the services-config shape of a schedule: `{every: N}`,
  `{daily: "HH:MM", tz: ZONE}` or `{calendar: EXPR}`; 200 `{}` on success, a refusal is a 4xx
  (400/403/409/422, 404 for a job that is not managed) with a `detail` message and writes nothing;
- the console kicks the recorder after a saved change (FakeSources.recorder_kicks);
- the unrecorded-changes entry of the job carries the new schedule as `schedule`, next to `paused`;
- while the entry is applying, `GET /api/services` shows `change: {state: "applying", pr, url}`
  and the job's schedule is still the box's (its timer's) own;
- `describe_schedule` shows a daily timer with a zone as "daily at HH:MM ZONE" and any other
  calendar as is. The deploy sync is faked by `apply()`: infra main's config is installed and the
  box's timer takes the new OnCalendar.
- `systemd-analyze` is faked on PATH: it rejects any calendar naming "tuesday-ish"."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml
from fastapi.testclient import TestClient

from dispatcher import services_record
from tests.infrafakes import FakeInfra, SERVICES, TOKEN
from tests.test_onboard import LIVE
from tests.test_services_config import base_config
from tests.test_web_services_acceptance import CLOCK, USER_DIR, root_timer
from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app

REFUSED = (400, 403, 409, 422)
DISPATCHER, UPDATE, ONBOARD, DIGEST, TRIAGE = (
    f"agent-ops-{j}" for j in ("dispatcher", "update", "onboard", "digest", "triage"))


def timer(job, calendar, description, **kw):
    return {"scope": "user", "unit": f"agent-ops-{job}.timer",
            "fragment_path": f"{USER_DIR}/agent-ops-{job}.timer", "description": description,
            "active_state": "active", "on_calendar": calendar,
            "last_run": None, "next_run": None, **kw}


def box_timers():
    return [timer("dispatcher", "*:0/5", "Dispatcher pass"),
            timer("update", "*:0/1", "Deploy sync"),
            timer("onboard", "*:0/1", "Onboarding pass"),
            timer("digest", "*-*-* 08:00:00", "Daily digest"),
            timer("triage", "*-*-* 04:00:00 Europe/Madrid", "Backlog triage"),
            root_timer("secret-cache-refresh")]


class World:
    def __init__(self, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "systemd-analyze"
        fake.write_text('#!/bin/sh\ncase "$*" in *tuesday-ish*) exit 1;; esac\nexit 0\n')
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("GH_INFRA_TOKEN", TOKEN)
        self.dir = tmp_path
        self.box = tmp_path / "targets.yaml"
        self.box.write_text(LIVE)
        self.cfg = base_config()
        self.cfg["update"]["schedule"] = {"every": 1}
        self.cfg["onboard"]["schedule"] = {"every": 1}
        self.cfg["triage"]["schedule"] = {"daily": "04:00", "tz": "Europe/Madrid"}
        (tmp_path / "services.yaml").write_text(yaml.safe_dump(self.cfg))
        self.infra = FakeInfra(self.cfg)
        self.sources = FakeSources()
        self.sources.box_timers = box_timers()
        self.client = TestClient(create_app(make_config(tmp_path), self.sources, clock=lambda: CLOCK))

    def put(self, job, body):
        return self.client.put(f"/api/services/{job}/schedule", json=body, headers=HEADERS)

    def post(self, job, verb):
        return self.client.post(f"/api/services/{job}/{verb}", headers=HEADERS)

    def record(self, passes=1):
        for _ in range(passes):
            services_record.run(self.box, sh=self.infra, get=lambda url, headers: b"{}")

    def settle(self, passes=6):
        """Every open pull request passes its checks, and the recorder runs until it has merged."""
        for _ in range(passes):
            for number, pr in self.infra.prs.items():
                if pr["state"] == "OPEN":
                    self.infra.ci(number, "success")
            self.record()

    def apply(self, job, calendar, **timer_fields):
        """The deploy sync installs infra main's config; the box's timer follows it."""
        self.infra.deploy(self.dir)
        for t in self.sources.box_timers:
            if t["unit"] == f"{job}.timer":
                t.update(on_calendar=calendar, **timer_fields)

    def job(self, job_id):
        body = self.client.get("/api/services", headers=HEADERS).json()
        return next(j for j in body["managed"] if j["id"] == job_id)

    def changes(self):
        f = self.dir / "services-changes.yaml"
        return (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}

    def merged(self, key):
        return self.infra.main_config()[key]

    def nothing_recorded(self, kicks=0):
        self.record(passes=2)
        return (self.changes() == {} and not (self.dir / "services-changes.yaml").exists()
                and self.sources.recorder_kicks == kicks
                and self.infra.created == [] and self.infra.pushes == [])


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def saved_for_review(world, job="agent-ops-dispatcher", body=None):
    assert world.put(job, body or {"every": 10}).status_code == 200
    world.record()
    return world.infra.created[0]


def test_saving_every_10_minutes_opens_a_recording_pr_and_the_job_keeps_its_old_schedule(world):
    assert world.put(DISPATCHER, {"every": 10}).status_code == 200
    assert world.sources.recorder_kicks == 1

    world.record()

    assert len(world.infra.created) == 1
    number = world.infra.created[0]
    pr = world.infra.prs[number]
    assert pr["files"] == [SERVICES]
    pushed = yaml.safe_load(world.infra.snapshots[pr["head"]][SERVICES])
    assert pushed["dispatcher"]["schedule"] == {"every": 10}
    assert {k: v for k, v in pushed.items() if k != "dispatcher"} == \
        {k: v for k, v in world.cfg.items() if k != "dispatcher"}
    job = world.job(DISPATCHER)
    assert job["change"]["state"] == "applying" and job["change"]["pr"] == number
    assert job["schedule"] == "every 5 min"
    assert yaml.safe_load((world.dir / "services.yaml").read_text())["dispatcher"]["schedule"] == {"every": 5}


def test_once_merged_and_applied_the_job_shows_every_10_min_and_no_applying_change(world):
    world.put(DISPATCHER, {"every": 10})
    world.record()
    world.settle()
    assert world.merged("dispatcher")["schedule"] == {"every": 10}
    assert world.job(DISPATCHER)["change"]["state"] == "applying"  # merged, not yet installed
    assert world.job(DISPATCHER)["schedule"] == "every 5 min"

    world.apply("agent-ops-dispatcher", "*:0/10")
    world.record()

    job = world.job(DISPATCHER)
    assert job["schedule"] == "every 10 min" and job["change"] is None
    assert world.changes() == {}


def test_a_daily_time_with_a_timezone_is_recorded_and_runs_there_once_applied(world):
    assert world.put(TRIAGE, {"daily": "05:00", "tz": "Europe/Madrid"}).status_code == 200
    world.record()
    world.settle()
    assert world.merged("triage")["schedule"] == {"daily": "05:00", "tz": "Europe/Madrid"}

    world.apply("agent-ops-triage", "*-*-* 05:00:00 Europe/Madrid", next_run="2026-11-25T04:00:00+00:00")
    world.record()

    job = world.job(TRIAGE)
    assert job["schedule"] == "daily at 05:00 Europe/Madrid"
    assert job["next_run"] == "2026-11-25T04:00:00+00:00" and job["change"] is None


def test_a_custom_expression_opens_a_recording_pr_and_is_shown_once_applied(world):
    assert world.put(DIGEST, {"calendar": "Mon..Fri *-*-* 09:00"}).status_code == 200
    world.record()
    assert len(world.infra.created) == 1
    assert world.job(DIGEST)["schedule"] == "daily at 08:00 UTC"
    world.settle()
    assert world.merged("digest")["schedule"] == {"calendar": "Mon..Fri *-*-* 09:00"}

    world.apply("agent-ops-digest", "Mon..Fri *-*-* 09:00", next_run="2026-11-25T09:00:00+00:00")
    world.record()

    job = world.job(DIGEST)
    assert job["schedule"] == "Mon..Fri *-*-* 09:00" and job["change"] is None
    assert job["next_run"] == "2026-11-25T09:00:00+00:00"


@pytest.mark.parametrize("body", [
    {"every": 0}, {"every": -3}, {"every": 1.5}, {"every": "10"},
    {"daily": "25:00", "tz": "UTC"}, {"daily": "05:00", "tz": "Nowhere/City"}, {"daily": "05:00"},
    {}, {"every": 10, "calendar": "daily"}], ids=str)
def test_an_invalid_schedule_is_refused_with_a_message_and_opens_no_pull_request(world, body):
    res = world.put(ONBOARD, body)

    assert res.status_code in REFUSED and res.json()["detail"]
    assert world.nothing_recorded()
    assert world.job(ONBOARD)["schedule"] == "every 1 min"


def test_every_0_minutes_for_the_onboarding_pass_is_refused_and_writes_nothing(world):
    res = world.put(ONBOARD, {"every": 0})

    assert res.status_code in REFUSED and res.json()["detail"]
    assert world.nothing_recorded()


def test_a_refusal_reads_without_the_config_key_of_the_job(world):
    res = world.put(ONBOARD, {"every": 0})

    assert res.json()["detail"] == "every must be a whole number of at least 1"


def test_an_invalid_custom_expression_is_refused_and_the_schedule_is_unchanged(world):
    res = world.put(DIGEST, {"calendar": "every tuesday-ish"})

    assert res.status_code in REFUSED and res.json()["detail"]
    assert world.nothing_recorded()
    assert world.job(DIGEST)["schedule"] == "daily at 08:00 UTC"
    assert world.job(DIGEST)["change"] is None


@pytest.mark.parametrize("calendar", [
    "Mon..Fri *-*-* 09:00\nUnit=evil.service", "Mon..Fri *-*-* 09:00\r", "*-*-* 09:00\x00",
    "*-*-* 09:00\x1b[2J", "", "   ", "-h"], ids=repr)
def test_a_custom_expression_with_a_line_break_or_control_character_sent_to_the_route_writes_nothing(
        world, calendar):
    res = world.put(DIGEST, {"calendar": calendar})

    assert res.status_code in REFUSED
    assert world.nothing_recorded()


def test_while_a_change_is_applying_a_second_save_is_refused_and_pause_run_and_stop_still_work(world):
    number = saved_for_review(world)
    entry = dict(world.changes()["dispatcher"])

    second = world.put(DISPATCHER, {"every": 15})

    assert second.status_code in REFUSED and second.json()["detail"]
    assert world.changes()["dispatcher"]["schedule"] == {"every": 10}
    world.record(passes=2)
    assert world.infra.created == [number]
    assert world.job(DISPATCHER)["change"]["state"] == "applying"
    assert entry["schedule"] == {"every": 10}
    for verb in ("pause", "run", "stop", "resume"):
        assert world.post(DISPATCHER, verb).status_code == 200
    assert world.put(DISPATCHER, {"every": 15}).status_code in REFUSED


def test_a_pause_made_while_a_schedule_change_applies_travels_in_the_same_entry_and_does_not_unlock(world):
    saved_for_review(world, DIGEST, {"every": 10})

    assert world.post(DIGEST, "pause").status_code == 200

    entry = world.changes()["digest"]
    assert entry["schedule"] == {"every": 10} and entry["paused"] is True
    assert world.job(DIGEST)["change"]["state"] == "applying"
    assert world.put(DIGEST, {"every": 15}).status_code in REFUSED
    world.settle(passes=10)
    assert world.merged("digest") == {**world.cfg["digest"], "schedule": {"every": 10}, "paused": True}


def test_once_the_change_has_applied_the_editor_can_be_saved_again(world):
    world.put(DISPATCHER, {"every": 10})
    world.record()
    world.settle()
    world.apply("agent-ops-dispatcher", "*:0/10")
    world.record()
    assert world.job(DISPATCHER)["change"] is None
    kicks = world.sources.recorder_kicks

    assert world.put(DISPATCHER, {"every": 15}).status_code == 200

    assert world.sources.recorder_kicks == kicks + 1
    assert world.changes()["dispatcher"]["schedule"] == {"every": 15}


def test_a_schedule_change_that_fails_its_checks_shows_change_failed_and_unlocks_the_editor(world):
    number = saved_for_review(world)
    world.infra.ci(number, "failure")

    world.record()

    job = world.job(DISPATCHER)
    assert job["change"]["state"] == "failed" and job["change"]["pr"] == number
    assert job["change"]["url"] == f"https://github.com/jesdi/agent-ops-infra/pull/{number}"
    assert job["schedule"] == "every 5 min" and world.infra.merged == []
    assert world.put(DISPATCHER, {"every": 15}).status_code == 200
    assert world.changes()["dispatcher"]["schedule"] == {"every": 15}
    assert world.job(DISPATCHER)["change"]["state"] != "failed"


@pytest.mark.parametrize("job_id", ["secret-cache-refresh", "agent-ops-secrets-refresh", "no-such-job"])
def test_a_schedule_save_for_a_root_job_is_refused_and_writes_nothing(world, job_id, tmp_path, monkeypatch):
    # Prove the PUT route exists in a separate world: the refusal must not be a missing route.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probing = World(probe_dir, monkeypatch)
    assert probing.put(DIGEST, {"every": 10}).status_code == 200
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(world.dir))

    res = world.put(job_id, {"every": 10})

    assert res.status_code in (400, 403, 404, 422)
    world.record(passes=2)
    assert world.nothing_recorded()


def test_while_the_deploy_sync_is_paused_a_schedule_save_is_refused_but_pause_still_works(world):
    assert world.post(UPDATE, "pause").status_code == 200
    world.record()
    created, kicks = list(world.infra.created), world.sources.recorder_kicks

    res = world.put(DISPATCHER, {"every": 10})

    assert res.status_code in REFUSED
    detail = str(res.json()["detail"]).lower()
    assert "deploy sync" in detail and "paused" in detail
    world.record(passes=2)
    assert world.infra.created == created and world.sources.recorder_kicks == kicks
    assert "dispatcher" not in world.changes()
    assert world.job(DISPATCHER)["schedule"] == "every 5 min" and world.job(DISPATCHER)["change"] is None

    assert world.post(DIGEST, "pause").status_code == 200
    world.record()
    assert world.job(DIGEST)["state"] == "paused"
    assert len(world.infra.created) == len(created) + 1
    assert world.merged("digest")["paused"] is False  # not merged yet, but the pull request is open


def test_saves_racing_each_other_for_one_job_are_refused_not_merged(world):
    racers = [{"every": n} for n in (10, 15, 20, 30, 45, 60, 90, 120)]
    gate = threading.Barrier(len(racers))

    def save(body):
        gate.wait()
        return body, world.put(DISPATCHER, body)

    with ThreadPoolExecutor(len(racers)) as pool:
        results = list(pool.map(save, racers))

    won = [body for body, r in results if r.status_code == 200]
    refused = [r for _, r in results if r.status_code != 200]
    assert len(won) == 1
    assert len(refused) == len(racers) - 1
    assert all(r.status_code in REFUSED and r.json()["detail"] for r in refused)
    assert world.changes()["dispatcher"]["schedule"] == won[0]
    world.record(passes=2)
    assert len(world.infra.created) == 1
    pushed = yaml.safe_load(world.infra.snapshots[world.infra.prs[world.infra.created[0]]["head"]][SERVICES])
    assert pushed["dispatcher"]["schedule"] == won[0]


def test_a_pause_still_being_recorded_is_not_an_applying_change_and_leaves_the_editor_unlocked(world):
    """Requirement 17 locks the editor for a schedule change only; the paused job shows "paused"."""
    assert world.post(DIGEST, "pause").status_code == 200
    world.record()
    assert world.changes()["digest"]["record"]["pr"]

    job = world.job(DIGEST)
    assert job["state"] == "paused" and job["change"] is None

    assert world.put(DIGEST, {"every": 10}).status_code == 200
    entry = world.changes()["digest"]
    assert entry["schedule"] == {"every": 10} and entry["paused"] is True
    assert world.job(DIGEST)["change"]["state"] == "applying"
