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


def test_answers_request_serves_readable_content(tmp_path):
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    qa_file = wt / ".agent" / "questionnaire.md"
    qa_file.parent.mkdir(parents=True)
    qa_file.write_text("## Questions\n\n1. What?")
    (tmp_path / "task-alpha-11.json").write_text(json.dumps({
        "issue": 11, "target": "alpha", "stage": "spec",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-11", "title": "Questionnaire task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "parked",
        "artifact": str(qa_file),
        "operator_request": {"kind": "answers", "path": ".agent/questionnaire.md"},
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/11/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "answers",
        "content": {
            "kind": "readable",
            "path": ".agent/questionnaire.md",
            "media_type": "text/markdown",
            "text": "## Questions\n\n1. What?",
        },
    }


def test_unknown_operator_request_kind_returns_500(tmp_path):
    """M2: an unrecognized kind in operator_request must be a server error, not silent 200 null."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    (tmp_path / "task-alpha-12.json").write_text(json.dumps({
        "issue": 12, "target": "alpha", "stage": "spec",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-12", "title": "Unknown kind task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "parked",
        "operator_request": {"kind": "totally-unknown"},
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/12/request", headers=HEADERS)

    assert response.status_code == 500


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
