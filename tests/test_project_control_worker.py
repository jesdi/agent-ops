"""A control request is advanced by the worker and never enters host provisioning."""
import pytest

from tests.test_project_tabs_controls_acceptance import Rig


def test_control_request_is_advanced_by_the_worker_and_never_provisioned(tmp_path):
    rig = Rig(tmp_path, host=object())
    op = rig.request("analytics", "pause").json()["operation"]
    assert "provisioning" not in rig.pc.status(op)
    assert rig.pc.verification().pending() == []
    with pytest.raises(ValueError):
        rig.pc.provision(op, 0)
    for _ in range(3):
        rig.pc.advance_controls()
    rig.dep.roll_out()
    rig.pc.advance_controls()
    assert rig.pc.status(op)["phase"] == "complete"
    assert rig.pc.verification().pending() == []
    assert rig.tabs()["analytics"]["state"] == "paused"


def test_the_worker_survives_a_failing_pass():
    import threading

    from web.__main__ import _advance_until

    stop, calls = threading.Event(), []

    class Control:
        def advance_controls(self):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")
            stop.set()

    _advance_until(stop, Control(), interval=0)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["create", "checks", "merge"])
def test_failed_project_does_not_starve_other_controls(tmp_path, monkeypatch, failure):
    from web.__main__ import _advance_once

    rig = Rig(tmp_path, with_shop=True)
    operations = [rig.request("analytics", "pause").json()["operation"],
                  rig.request("shop", "enable").json()["operation"]]
    first, second = sorted(operations)
    failed_name = rig.pc._load(first)["plan"]["name"]
    method = {"create": "create_pr", "checks": "checks", "merge": "merge"}[failure]
    original = getattr(rig.gh, method)

    def fail_first(*args):
        branch = args[0] if failure == "create" else rig.gh.prs[args[0]]["branch"]
        if first in branch:
            if failure == "merge":
                original(*args)  # the write succeeded, but its response was lost
            raise ConnectionError("adapter unavailable")
        return original(*args)

    monkeypatch.setattr(rig.gh, method, fail_first)
    _advance_once(rig.pc)
    assert rig.pc.status(second)["phase"] == "merged"
    failed = rig.tabs()[failed_name]
    assert failed["pending"]["phase"] == "blocked"
    assert "adapter unavailable" in failed["pending"]["blocker"]
    assert failed["state"] == ("enabled" if failed_name == "analytics" else "paused")
    if failure == "merge":
        assert rig.pc._load(first)["merging"] is True
        _advance_once(rig.pc)
        assert len(rig.gh.merges) == 2  # never repeat the uncertain merge
    else:
        monkeypatch.setattr(rig.gh, method, original)
        _advance_once(rig.pc)
        assert "adapter unavailable" not in rig.pc.status(first)["blocker"]
        assert len(rig.gh.created) == 2


def test_blocked_control_can_be_reconfirmed_through_the_project_api(tmp_path):
    rig = Rig(tmp_path)
    operation = rig.request("analytics", "pause").json()["operation"]
    rig.gh.config["projects"]["analytics"]["models"] = {"pinned": ["updated-model"]}
    rig.pc.advance_controls()
    assert rig.tabs()["analytics"]["pending"]["phase"] == "blocked"
    assert rig.request("analytics", "pause").json()["operation"] == operation
    rig.pc.advance_controls()
    assert rig.pc.status(operation)["phase"] == "merged"
    assert rig.tabs()["analytics"]["state"] == "enabled"
    rig.dep.roll_out()
    rig.pc.advance_controls()
    assert rig.tabs()["analytics"]["state"] == "paused"
    assert rig.gh.config["projects"]["analytics"]["models"] == {"pinned": ["updated-model"]}
