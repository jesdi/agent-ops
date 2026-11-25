"""Acceptance (services-section ticket 08): the recorder turns a console pause or resume into
a pull request on the infra repo that merges by itself.

Black-box: `dispatcher.services_record.run(box, sh=, get=)` with the fake infra repo of
tests/infrafakes.py, and the console through TestClient (`POST /api/services/{job}/pause`,
`/resume`, `/retry`, `GET /api/services`). Interface defined here (the ticket leaves it open):
- `GET /api/services` gives each job `change`: null, or `{state: "failed"|"applying",
  pr: int|null, url: str|null}`; "failed" is a recording that could not be opened or merged.
- `POST /api/services/{job}/retry` clears a failed mark and kicks the recorder.
- the console kicks the recorder after every pause, resume and retry (FakeSources.recorder_kicks).
- the installed config `<state_dir>/services.yaml` is what drops an entry; the infra repo's
  services config is `config/services.yaml` (see infrafakes)."""
import yaml
import pytest
from fastapi.testclient import TestClient

from dispatcher import services_record
from tests.infrafakes import FakeInfra, SERVICES, TOKEN
from tests.test_onboard import LIVE
from tests.test_services_config import base_config
from tests.test_web_services_acceptance import CLOCK, user_timer
from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app

DIGEST = "agent-ops-digest"


class World:
    def __init__(self, tmp_path, monkeypatch, digest_paused=False):
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("GH_INFRA_TOKEN", TOKEN)
        self.dir = tmp_path
        self.box = tmp_path / "targets.yaml"
        self.box.write_text(LIVE)
        cfg = base_config()
        cfg["digest"]["paused"] = digest_paused
        (tmp_path / "services.yaml").write_text(yaml.safe_dump(cfg))
        self.infra = FakeInfra(cfg)
        self.sources = FakeSources()
        self.sources.box_timers = [user_timer(DIGEST, active_state="inactive" if digest_paused else "active")]
        self.client = TestClient(create_app(make_config(tmp_path), self.sources, clock=lambda: CLOCK))
        self.gets = []

    def post(self, verb):
        return self.client.post(f"/api/services/{DIGEST}/{verb}", headers=HEADERS)

    def record(self, passes=1):
        for _ in range(passes):
            services_record.run(self.box, sh=self.infra, get=lambda url, headers: self.gets.append(url) or b"{}")

    def job(self):
        body = self.client.get("/api/services", headers=HEADERS).json()
        return next(j for j in body["managed"] if j["id"] == DIGEST)

    def changes(self):
        f = self.dir / "services-changes.yaml"
        return (yaml.safe_load(f.read_text()) or {}) if f.exists() else {}

    def merged_config(self):
        return self.infra.main_config()["digest"]["paused"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def only_services_changed(infra):
    return all(files == [SERVICES] for _, _, _, files in infra.merged) and \
        all(diff == [SERVICES] for _, _, diff in infra.pushes)


def test_a_pause_is_recorded_by_one_pull_request_that_changes_only_the_services_config(world):
    world.post("pause")

    world.record()

    assert len(world.infra.created) == 1
    pr = world.infra.prs[world.infra.created[0]]
    assert pr["files"] == [SERVICES]
    pushed = world.infra.snapshots[pr["head"]]
    assert yaml.safe_load(pushed[SERVICES])["digest"]["paused"] is True
    assert {k: v for k, v in yaml.safe_load(pushed[SERVICES]).items() if k != "digest"} == \
        {k: v for k, v in base_config().items() if k != "digest"}
    assert world.sources.recorder_kicks == 1


def test_a_pull_request_whose_ci_succeeded_merges_on_the_next_pass(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    assert world.infra.merged == []
    world.infra.ci(number, "success")

    world.record()

    assert [m[0] for m in world.infra.merged] == [number]
    assert world.merged_config() is True
    assert only_services_changed(world.infra)
    assert world.infra.merged[0][2] == "success"


def test_the_entry_stays_until_the_installed_config_says_paused(world):
    world.post("pause")
    world.record()
    world.infra.ci(world.infra.created[0], "success")
    world.record()
    assert world.merged_config() is True

    world.record()  # merged, but the deploy sync has not installed it yet
    assert world.changes()["digest"]["paused"] is True
    assert world.job()["state"] == "paused"

    world.infra.deploy(world.dir)
    world.record()

    assert "digest" not in world.changes()
    job = world.job()
    assert job["state"] == "paused" and job["change"] is None


def test_a_resume_is_recorded_by_a_pull_request_setting_not_paused(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch, digest_paused=True)
    world.post("resume")

    world.record()

    assert len(world.infra.created) == 1
    pr = world.infra.prs[world.infra.created[0]]
    assert pr["files"] == [SERVICES]
    assert yaml.safe_load(world.infra.snapshots[pr["head"]][SERVICES])["digest"]["paused"] is False
    world.infra.ci(pr["number"], "success")
    world.record()
    assert world.merged_config() is False


def test_pause_then_resume_before_the_merge_closes_the_pull_request_and_never_merges_paused(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.post("resume")
    world.infra.ci(number, "success")

    world.record(passes=3)

    assert world.infra.closed == [number]
    assert world.infra.merged == []
    assert world.merged_config() is False
    assert world.job()["state"] == "active"


def test_a_failing_ci_run_shows_change_failed_with_the_pull_request_and_does_not_merge(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.ci(number, "failure")

    world.record()

    assert world.infra.merged == []
    change = world.job()["change"]
    assert change["state"] == "failed" and change["pr"] == number
    assert change["url"] == f"https://github.com/jesdi/agent-ops-infra/pull/{number}"


def test_an_unreachable_github_leaves_the_job_paused_and_shows_change_failed(world):
    world.infra.down = True

    assert world.post("pause").status_code == 200
    world.record()

    job = world.job()
    assert job["state"] == "paused"
    assert job["change"]["state"] == "failed" and job["change"]["url"] is None
    assert world.changes()["digest"]["paused"] is True


def test_a_failed_change_is_not_retried_by_30_minutes_of_passes(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.ci(number, "failure")
    world.record()
    pushes, created = len(world.infra.pushes), len(world.infra.created)

    world.record(passes=30)

    assert (len(world.infra.pushes), len(world.infra.created)) == (pushes, created)
    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_retry_after_github_is_back_records_the_pause_and_clears_change_failed(world):
    world.infra.down = True
    world.post("pause")
    world.record()
    assert world.job()["change"]["state"] == "failed"
    world.infra.down = False
    kicks = world.sources.recorder_kicks

    assert world.client.post(f"/api/services/{DIGEST}/retry", headers=HEADERS).status_code == 200
    assert world.sources.recorder_kicks == kicks + 1
    world.record()

    assert len(world.infra.created) == 1
    number = world.infra.created[0]
    assert yaml.safe_load(world.infra.snapshots[world.infra.prs[number]["head"]][SERVICES])["digest"]["paused"] is True
    world.infra.ci(number, "success")
    world.record()
    world.infra.deploy(world.dir)
    world.record()
    assert world.job()["change"] is None and world.job()["state"] == "paused"


def test_a_pull_request_on_the_branch_that_touches_another_file_is_not_merged(world):
    world.infra.rogue = {"config/targets.yaml": "projects: [pwned]\n"}
    world.post("pause")

    world.record(passes=3)

    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_a_pull_request_with_the_saved_head_whose_diff_has_another_file_is_not_merged(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.prs[number]["files"] = [SERVICES, "config/targets.yaml"]
    world.infra.ci(number, "success")

    world.record(passes=3)

    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_another_pull_request_on_the_saved_head_is_not_merged(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.ci(number, "success")  # ci runs belong to the head, whichever PR carries it
    original = world.infra.prs[number]
    original["state"] = "CLOSED"
    world.infra.prs[99] = {**original, "number": 99, "state": "OPEN"}

    world.record(passes=3)

    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_a_resume_during_a_pass_never_merges_the_pause(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.ci(number, "success")
    resumed = []

    def sh(argv, **kw):
        if not resumed and "actions/runs?head_sha=" in " ".join(argv):
            resumed.append(world.post("resume").status_code)
        return world.infra(argv, **kw)

    services_record.run(world.box, sh=sh, get=lambda url, headers: b"{}")
    world.record(passes=3)

    assert resumed == [200]
    assert world.infra.merged == []
    assert world.merged_config() is False
    assert world.job()["state"] == "active"


def test_main_advancing_under_the_open_pull_request_rebuilds_it_before_it_merges(world):
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    old_head = world.infra.prs[number]["head"]
    world.infra.ci(number, "success")
    world.infra.main["config/targets.yaml"] = "projects: [new]\n"
    world.infra._main_version += 1

    world.record()
    assert world.infra.merged == []
    world.record()
    assert world.infra.merged == []
    world.infra.ci(number, "success")
    world.record()

    assert [(m[0], m[2]) for m in world.infra.merged] == [(number, "success")]
    assert world.infra.merged[0][1] != old_head
    assert world.infra.merged[0][3] == [SERVICES]
    assert world.infra.main["config/targets.yaml"] == "projects: [new]\n"
    assert world.merged_config() is True


def test_overlapping_recorder_passes_open_one_pull_request(world):
    world.post("pause")
    nested = []

    def sh(argv, **kw):
        if not nested and "push" in argv:
            nested.append(1)
            world.record()  # a second pass starts while this one is pushing
        return world.infra(argv, **kw)

    services_record.run(world.box, sh=sh, get=lambda url, headers: b"{}")

    assert nested == [1]
    assert len(world.infra.created) == 1 and len(world.infra.pushes) == 1
    assert world.job()["change"] is None  # a pause being recorded is not a schedule applying


def test_without_an_infra_token_the_job_shows_change_failed_and_stays_paused(world, monkeypatch):
    monkeypatch.delenv("GH_INFRA_TOKEN")
    world.post("pause")

    world.record()

    assert world.infra.created == [] and world.infra.merged == []
    job = world.job()
    assert job["state"] == "paused" and job["change"]["state"] == "failed"
    assert world.changes()["digest"]["paused"] is True


def test_the_console_kicks_the_recorder_after_pause_and_resume(world):
    world.post("pause")
    world.post("resume")

    assert world.sources.recorder_kicks == 2
