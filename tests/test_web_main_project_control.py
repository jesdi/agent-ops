import sys
import types

from web.__main__ import _project_control


def test_project_control_comes_from_the_named_factory(monkeypatch, tmp_path):
    box = types.ModuleType("fake_box")
    box.build = lambda state_dir: ("control", state_dir)
    monkeypatch.setitem(sys.modules, "fake_box", box)
    monkeypatch.delenv("AGENT_OPS_PROJECT_CONTROL", raising=False)
    assert _project_control(str(tmp_path)) is None
    monkeypatch.setenv("AGENT_OPS_PROJECT_CONTROL", "fake_box:build")
    assert _project_control(str(tmp_path)) == ("control", str(tmp_path))


def test_a_blocked_control_request_is_rebased_by_repeating_it(tmp_path):
    from tests.test_project_control_pr_deployment_acceptance import Deployer, Github
    from project_control import ProjectControl
    gh = Github(tmp_path / "state")
    gh.config["projects"]["billing"].update(board=None, repositories=[{"repo": "example/billing"}])
    control = ProjectControl(str(tmp_path / "state"), gh, Deployer(tmp_path / "state", gh))
    op = control.request_enabled("billing", False)
    gh.config["projects"]["billing"]["models"] = "changed"
    gh.sha = "s2"
    control.advance(op)
    assert "supersede" in control.status(op)["blocker"]
    assert control.request_enabled("billing", False) == op
    control.advance(op)
    assert "supersede" not in control.status(op)["blocker"]


def test_a_malformed_live_configuration_keeps_the_last_good_one(tmp_path):
    from fastapi.testclient import TestClient
    from dispatcher.config import load_config
    from tests.webfakes import HEADERS, FakeSources
    from web.app import create_app
    path = tmp_path / "targets.yaml"
    path.write_text(f"state_dir: {tmp_path}\ntargets: []\n")
    client = TestClient(create_app(load_config(path), FakeSources(str(tmp_path)), config_path=str(path)))
    for broken in ("", "a: [unclosed"):
        path.write_text(broken)
        assert client.get("/api/projects", headers=HEADERS).status_code == 200
