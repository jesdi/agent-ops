"""Recorder races with the console: a Resume landing mid-pass or before the deploy, and a
pull request on the branch that the recorder never saved as its own."""
import yaml

from dispatcher import services_record
from tests.infrafakes import SERVICES, failure
from tests.test_services_record_acceptance import World


def test_a_resume_while_the_pull_request_is_created_leaves_no_open_pull_request(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.post("pause")

    def sh(argv, **kw):
        if "create" in argv:
            assert world.post("resume").status_code == 200
        return world.infra(argv, **kw)

    services_record.run(world.box, sh=sh, get=lambda url, headers: b"{}")
    world.record(passes=2)

    assert world.infra.created and all(p["state"] != "OPEN" for p in world.infra.prs.values())
    assert world.infra.merged == [] and world.changes() == {}


def test_a_resume_after_the_merge_but_before_the_deploy_is_recorded(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.post("pause")
    world.record()
    world.infra.ci(world.infra.created[0], "success")
    world.record(passes=2)  # merged; the installed config still says not paused
    assert world.merged_config() is True

    world.post("resume")
    world.record()
    world.infra.ci(world.infra.created[-1], "success")
    world.record()

    assert len(world.infra.created) == 2 and world.merged_config() is False
    world.infra.deploy(world.dir)
    world.record()
    assert world.changes() == {}


def test_a_pull_request_on_the_pushed_head_that_the_recorder_did_not_open_is_not_merged(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.post("pause")

    def sh(argv, **kw):
        if "create" in argv:
            raise failure("gh: connection reset")
        return world.infra(argv, **kw)

    services_record.run(world.box, sh=sh, get=lambda url, headers: b"{}")
    assert world.job()["change"]["state"] == "failed" and world.infra.created == []
    world.infra._create(["--head", "services/digest"])  # someone else opens it
    world.infra.ci(world.infra.created[0], "success")
    world.post("retry")

    world.record(passes=3)

    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_retrying_a_failure_on_another_pull_request_never_merges_it(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.post("pause")
    world.record()
    number = world.infra.created[0]
    world.infra.ci(number, "success")
    original = world.infra.prs[number]
    original["state"] = "CLOSED"
    world.infra.prs[99] = {**original, "number": 99, "state": "OPEN"}
    world.record()
    assert world.job()["change"] == {"state": "failed", "pr": 99, "url": world.infra._row(original | {"number": 99})["url"]}

    for _ in range(3):
        world.post("retry")
        world.record()

    assert world.infra.merged == []
    assert world.job()["change"]["state"] == "failed"


def test_infra_main_already_matching_leaves_a_pull_request_the_recorder_did_not_open(tmp_path, monkeypatch):
    world = World(tmp_path, monkeypatch)
    world.post("pause")
    world.infra.rogue = {"config/other.yaml": "x: 1\n"}
    cfg = world.infra.main_config()
    cfg["digest"]["paused"] = True
    world.infra.main[SERVICES] = yaml.safe_dump(cfg)

    world.record(passes=2)

    assert world.infra.closed == [] and world.infra.prs[99]["state"] == "OPEN"
