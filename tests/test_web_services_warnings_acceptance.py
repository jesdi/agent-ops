"""Acceptance (services-section ticket 11): a warning for a job that differs from the config,
and the managed group ordered by need of a look. Black-box: `GET /api/services` through TestClient.

Interface defined here (the ticket leaves it open): every managed job of the response carries
`warning`: null, or a message saying how the box differs from the installed
services config (`<state_dir>/services.yaml`) and no console change (an entry of
`<state_dir>/services-changes.yaml`) is behind it. Job ids are `agent-ops-<config key>`."""
import yaml

from tests.test_services_config import base_config
from tests.test_web_services_acceptance import user_timer
from tests.test_web_services_pause_acceptance import box, job, post
from tests.test_web_services_schedule_acceptance import (
    DIGEST, DISPATCHER, ONBOARD, TRIAGE, world)  # noqa: F401  (world is a fixture)
from tests.webfakes import HEADERS

SWEEP = "agent-ops-sweep"
KEYS = ["dispatcher", "update", "onboard", "keepalive", "codex-keepalive", "sweep", "digest", "triage"]


def sweep_timer(**kw):
    t = user_timer("agent-ops-worktree-sweep", **kw)
    t.update(unit=f"{SWEEP}.timer", fragment_path=t["fragment_path"].replace("worktree-sweep", "sweep"))
    return t


def warning_of(client, job_id):
    j = job(client, job_id)
    assert "warning" in j
    return j["warning"]


def test_a_timer_switched_off_by_hand_shows_paused_with_a_warning_that_the_box_differs(tmp_path):
    _, client = box(tmp_path, [sweep_timer(active_state="inactive")])

    j = job(client, SWEEP)

    assert j["state"] == "paused"
    assert j["warning"] == "Paused on the box but not in the services config."


def test_a_timer_switched_on_by_hand_says_the_config_has_it_paused(tmp_path):
    cfg = base_config()
    cfg["triage"]["paused"] = True
    _, client = box(tmp_path, [user_timer("agent-ops-triage")])
    (tmp_path / "services.yaml").write_text(yaml.safe_dump(cfg))

    assert warning_of(client, TRIAGE) == "Active on the box but paused in the services config."


def test_a_pause_still_being_recorded_shows_paused_with_no_warning(tmp_path):
    _, client = box(tmp_path, [user_timer("agent-ops-digest")])
    assert post(client, DIGEST, "pause").status_code == 200

    assert job(client, DIGEST)["state"] == "paused"
    assert warning_of(client, DIGEST) is None


def test_a_job_the_config_says_is_paused_and_whose_timer_is_off_has_no_warning(tmp_path):
    cfg = base_config()
    cfg["triage"]["paused"] = True
    fake, client = box(tmp_path, [user_timer("agent-ops-triage", active_state="inactive")])
    (tmp_path / "services.yaml").write_text(yaml.safe_dump(cfg))

    assert job(client, TRIAGE)["state"] == "paused"
    assert warning_of(client, TRIAGE) is None


def test_a_box_with_no_installed_config_flags_no_job(tmp_path):
    _, client = box(tmp_path, [sweep_timer(active_state="inactive"), user_timer("agent-ops-digest")],
                    installed=False)

    assert warning_of(client, SWEEP) is None
    assert warning_of(client, DIGEST) is None


def test_the_group_lists_the_hand_edited_job_first_then_paused_jobs_then_the_active_ones(tmp_path):
    cfg = base_config()
    cfg["digest"]["paused"] = cfg["triage"]["paused"] = True
    timers = []
    for key in KEYS:
        unit = f"agent-ops-{key}"
        t = sweep_timer() if key == "sweep" else user_timer("agent-ops-digest")
        t.update(unit=f"{unit}.timer", fragment_path=f"/home/agent/.config/systemd/user/{unit}.timer",
                 description=key, active_state="inactive" if key in ("sweep", "digest", "triage") else "active")
        timers.append(t)
    _, client = box(tmp_path, timers)
    (tmp_path / "services.yaml").write_text(yaml.safe_dump(cfg))

    managed = client.get("/api/services", headers=HEADERS).json()["managed"]

    assert [j["id"] for j in managed] == [f"agent-ops-{k}" for k in
        ("sweep", "digest", "triage", "dispatcher", "update", "onboard", "keepalive", "codex-keepalive")]
    assert [j["warning"] is not None for j in managed][:3] == [True, False, False]


def _world_with_onboard_paused(world):
    assert world.post(ONBOARD, "pause").status_code == 200
    world.record()
    world.settle()
    world.infra.deploy(world.dir)
    world.record()
    assert world.changes() == {}
    assert world.job(ONBOARD)["state"] == "paused"


def _ids(world):
    return [j["id"] for j in world.client.get("/api/services", headers=HEADERS).json()["managed"]]


def test_a_job_applying_a_change_is_listed_before_paused_jobs(world):
    _world_with_onboard_paused(world)
    assert world.put(TRIAGE, {"daily": "05:00", "tz": "Europe/Madrid"}).status_code == 200
    world.record()
    assert world.job(TRIAGE)["change"]["state"] == "applying"

    assert _ids(world) == [TRIAGE, ONBOARD, DISPATCHER, "agent-ops-update", DIGEST]


def test_a_job_whose_change_failed_is_listed_before_paused_jobs(world):
    _world_with_onboard_paused(world)
    assert world.put(TRIAGE, {"every": 10}).status_code == 200
    world.record()
    world.infra.ci(world.infra.created[-1], "failure")
    world.record()
    assert world.job(TRIAGE)["change"]["state"] == "failed"

    assert _ids(world) == [TRIAGE, ONBOARD, DISPATCHER, "agent-ops-update", DIGEST]
