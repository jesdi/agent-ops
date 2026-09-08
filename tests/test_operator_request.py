"""First red acceptance slice for the operator-request refactor.

Exercise HTTP through real task loading and file reads. The legacy record
is intentional: an existing spec-review task must survive the refactor.
"""
import json

import pytest
from fastapi.testclient import TestClient

from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources


@pytest.mark.parametrize("park", ["", "awaiting-review"], ids=["live-gate", "parked-gate"])
def test_existing_spec_gate_exposes_an_explicit_approval_request(tmp_path, park):
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    spec = wt / "docs" / "specs" / "login-design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Login redirect design\n\nUse the staging host.")
    agent_dir = wt / ".agent"
    agent_dir.mkdir()
    (agent_dir / "stage.json").write_text(json.dumps({
        "stage": "spec", "status": "awaiting-review",
        "artifact": "docs/specs/login-design.md",
    }))
    (tmp_path / "task-alpha-7.json").write_text(json.dumps({
        "issue": 7, "target": "alpha", "stage": "awaiting-spec-review",
        "slot": -1 if park else 0, "worktree": str(wt),
        "branch": "agent/task-7", "title": "Fix login redirect",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": park, "artifact": str(spec),
    }))
    # This read requires neither a running session nor a GitHub request.
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/7/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "spec-approval",
        "content": {
            "kind": "readable",
            "path": "docs/specs/login-design.md",
            "media_type": "text/markdown",
            "text": "# Login redirect design\n\nUse the staging host.",
        },
    }


def test_existing_non_gate_task_returns_null_request(tmp_path):
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    (tmp_path / "task-alpha-9.json").write_text(json.dumps({
        "issue": 9, "target": "alpha", "stage": "implement",
        "slot": 0, "worktree": str(wt),
        "branch": "agent/task-9", "title": "Add dark mode",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "", "artifact": "",
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/9/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() is None
