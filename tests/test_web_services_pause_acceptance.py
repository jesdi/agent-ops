"""Acceptance: `POST /api/services/{job}/pause` and `/resume`. Driven through
create_app with FakeSources, whose `disable_timer` / `enable_timer` are the fake
`systemctl --user disable|enable --now <job>.timer` (recorded in `systemctl_calls`
as ("disable"|"enable", "<job>.timer"); `systemctl_failure` makes them raise).

Interface defined here (the ticket leaves it open): the unrecorded-changes file is
`<state_dir>/services-changes.yaml`, a YAML map keyed by the services-config job
name (`agent-ops-digest` -> `digest`) holding `{paused: bool}`; the installed
services config is `<state_dir>/services.yaml` (same shape as ticket 04)."""
import pytest
import yaml
from fastapi.testclient import TestClient

from tests.test_services_config import base_config
from tests.test_web_services_acceptance import CLOCK, full_box, user_timer
from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app

DIGEST = "agent-ops-digest"


def box(tmp_path, timers, installed=True, changes=None):
    fake = FakeSources()
    fake.box_timers = timers
    if installed:
        (tmp_path / "services.yaml").write_text(yaml.safe_dump(base_config()))
    if changes is not None:
        (tmp_path / "services-changes.yaml").write_text(yaml.safe_dump(changes))
    client = TestClient(create_app(make_config(tmp_path), fake, clock=lambda: CLOCK))
    return fake, client


def recorded(tmp_path):
    f = tmp_path / "services-changes.yaml"
    return (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}


def job(client, job_id):
    body = client.get("/api/services", headers=HEADERS).json()
    return next(j for j in body["managed"] if j["id"] == job_id)


def post(client, job_id, verb):
    return client.post(f"/api/services/{job_id}/{verb}", headers=HEADERS)


def test_pause_stops_the_timer_shows_paused_and_records_the_change(tmp_path):
    digest = user_timer(DIGEST, next_run="2026-11-25T08:00:00+00:00")
    fake, client = box(tmp_path, [digest])

    assert post(client, DIGEST, "pause").status_code == 200

    assert fake.systemctl_calls == [("disable", f"{DIGEST}.timer")]
    assert job(client, DIGEST)["state"] == "paused"
    assert recorded(tmp_path)["digest"]["paused"] is True


def test_resume_starts_the_timer_and_shows_active(tmp_path):
    digest = user_timer(DIGEST, active_state="inactive")
    fake, client = box(tmp_path, [digest], changes={"digest": {"paused": True}})

    assert post(client, DIGEST, "resume").status_code == 200

    assert fake.systemctl_calls == [("enable", f"{DIGEST}.timer")]
    assert job(client, DIGEST)["state"] == "active"


def test_pause_then_resume_leaves_no_entry_when_the_installed_config_is_not_paused(tmp_path):
    fake, client = box(tmp_path, [user_timer(DIGEST)])

    assert post(client, DIGEST, "pause").status_code == 200
    assert post(client, DIGEST, "resume").status_code == 200

    assert "digest" not in recorded(tmp_path)
    assert job(client, DIGEST)["state"] == "active"


@pytest.mark.parametrize("job_id", ["secret-cache-refresh", "tracker-backup",
                                    "agent-ops-herdr", "no-such-job"])
@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_a_job_that_is_not_managed_is_refused_with_no_systemctl_call_and_no_write(
        tmp_path, job_id, verb):
    probe = tmp_path / "probe"
    probe.mkdir()
    _, probing = box(probe, [user_timer(DIGEST)])
    assert post(probing, DIGEST, verb).status_code == 200  # the route exists
    fake, client = box(tmp_path, full_box())

    res = post(client, job_id, verb)

    assert res.status_code in (400, 403, 404, 422)
    assert fake.systemctl_calls == []
    assert not (tmp_path / "services-changes.yaml").exists()


def test_a_failing_systemctl_gives_an_error_leaves_the_job_active_and_records_nothing(tmp_path):
    fake, client = box(tmp_path, [user_timer(DIGEST)])
    fake.systemctl_failure = RuntimeError("systemctl failed")

    res = post(client, DIGEST, "pause")

    assert res.status_code >= 500
    assert job(client, DIGEST)["state"] == "active"
    assert "digest" not in recorded(tmp_path)


def test_overlapping_pause_and_resume_leave_one_entry_matching_the_timer(tmp_path):
    """The pause's systemctl call holds until a second systemctl call arrives (or
    0.5s passes), so unserialised requests would write in the opposite order of
    their systemctl calls."""
    import threading

    fake, client = box(tmp_path, [user_timer(DIGEST)])
    inside, second = threading.Event(), threading.Event()
    original = fake._switch_timer

    def slow(verb, unit, active_state):
        if verb == "disable":
            original(verb, unit, active_state)
            inside.set()
            second.wait(0.5)
        else:
            original(verb, unit, active_state)
            second.set()

    fake._switch_timer = slow
    results = {}
    pause = threading.Thread(target=lambda: results.update(pause=post(client, DIGEST, "pause")))
    pause.start()
    assert inside.wait(5)
    resume = threading.Thread(target=lambda: results.update(resume=post(client, DIGEST, "resume")))
    resume.start()
    pause.join(10)
    resume.join(10)

    assert results["pause"].status_code == 200 and results["resume"].status_code == 200
    paused = recorded(tmp_path).get("digest", {}).get("paused", False)
    assert (job(client, DIGEST)["state"] == "paused") == paused


NOT_INSTALLED = "The services config is not installed on the box yet."


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_with_no_services_config_installed_pause_and_resume_are_refused_and_change_nothing(tmp_path, verb):
    fake, client = box(tmp_path, [user_timer(DIGEST)], installed=False)

    res = post(client, DIGEST, verb)

    assert res.status_code == 409
    assert res.json()["detail"] == NOT_INSTALLED
    assert fake.systemctl_calls == [] and fake.recorder_kicks == 0
    assert not (tmp_path / "services-changes.yaml").exists()


def test_with_no_services_config_installed_a_schedule_save_is_refused_and_writes_nothing(tmp_path):
    fake, client = box(tmp_path, [user_timer(DIGEST)], installed=False)

    res = client.put(f"/api/services/{DIGEST}/schedule", json={"every": 10}, headers=HEADERS)

    assert res.status_code == 409
    assert res.json()["detail"] == NOT_INSTALLED
    assert fake.recorder_kicks == 0
    assert not (tmp_path / "services-changes.yaml").exists()


def test_with_no_services_config_installed_run_now_and_stop_run_still_work(tmp_path):
    fake, client = box(tmp_path, [user_timer(DIGEST)], installed=False)

    assert post(client, DIGEST, "run").status_code == 200
    assert post(client, DIGEST, "stop").status_code == 200
    assert fake.systemctl_calls == [("start", f"{DIGEST}.service"), ("stop", f"{DIGEST}.service")]


def test_the_page_is_told_whether_a_services_config_is_installed(tmp_path):
    _, missing = box(tmp_path, [user_timer(DIGEST)], installed=False)
    assert missing.get("/api/services", headers=HEADERS).json()["config_installed"] is False
    (tmp_path / "services.yaml").write_text(yaml.safe_dump(base_config()))
    assert missing.get("/api/services", headers=HEADERS).json()["config_installed"] is True
