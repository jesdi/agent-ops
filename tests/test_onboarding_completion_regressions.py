"""Readiness follows verified control deployments and the effective board."""
import pytest
import yaml

from project_control import ProjectControl
from tests.test_verified_onboarding_completion_acceptance import (
    _env, enable, make_rig, onboard, report,
)


def test_enabled_readiness_survives_unrelated_pending_deployment_and_restart(tmp_path):
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "analytics", "example/analytics")
    enable(rig, box, "analytics")
    deployed = rig.dep.revision
    rig.gh.sha = "unrelated-main-revision"
    rig.pc = ProjectControl(rig.pc.state_dir, rig.gh, rig.dep,
                            boards=rig.boards, verifier=rig.pc.verifier, host=host)
    out = report(box, "analytics")
    assert "Onboarding: complete" in out, out
    assert "Project: analytics - enabled" in out
    assert f"Applied revision: {deployed}" in out


@pytest.mark.parametrize("field,value", [("board_number", 9), ("board_owner", "another-owner")])
def test_readiness_rejects_wrong_effective_board(tmp_path, field, value):
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "analytics", "example/analytics")
    live = yaml.safe_load(rig.path.read_text())
    project = next(p for p in live["projects"] if p["key"] == "analytics")
    project[field] = value
    rig.path.write_text(yaml.safe_dump(live))
    out = report(box, "analytics")
    assert "Onboarding: incomplete" in out, out
    assert "Blocked: web: project analytics serves a different board" in out


@pytest.mark.parametrize("deployed", [None, "unverified-revision"])
def test_readiness_requires_evidence_for_the_actual_deployed_revision(tmp_path, deployed):
    rig, host, box = make_rig(tmp_path)
    onboard(rig, box, "analytics", "example/analytics")
    enable(rig, box, "analytics")
    rig.gh.sha = "unrelated-main-revision"
    rig.dep.revision = deployed
    out = report(box, "analytics")
    assert "Onboarding: incomplete" in out, out
    assert "Blocked: deployment:" in out
