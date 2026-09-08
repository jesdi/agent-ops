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


def test_spec_approval_missing_file_returns_200_unavailable(tmp_path):
    """Slice 7: spec file deleted after request was established → 200, content.kind=unavailable."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    spec = wt / "docs" / "specs" / "login-design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Spec")
    (tmp_path / "task-alpha-20.json").write_text(json.dumps({
        "issue": 20, "target": "alpha", "stage": "awaiting-spec-review",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-20", "title": "Missing spec task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "awaiting-review", "artifact": str(spec),
    }))
    spec.unlink()  # delete after task was saved
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/20/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "spec-approval"
    assert body["content"]["kind"] == "unavailable"
    assert "reason" in body["content"]


def test_answers_missing_file_returns_200_unavailable(tmp_path):
    """Slice 7: answers file missing → 200, content.kind=unavailable, request still visible."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    (tmp_path / "task-alpha-21.json").write_text(json.dumps({
        "issue": 21, "target": "alpha", "stage": "spec",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-21", "title": "Missing answers task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "parked",
        "operator_request": {"kind": "answers", "path": ".agent/gone.md"},
    }))
    # .agent/gone.md is never created — the file doesn't exist
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/21/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "answers"
    assert body["content"]["kind"] == "unavailable"
    assert "reason" in body["content"]


def test_answers_non_utf8_file_returns_200_unavailable(tmp_path):
    """Slice 7: non-UTF-8 answers file → 200, content.kind=unavailable."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    agent_dir = wt / ".agent"
    agent_dir.mkdir(parents=True)
    bad = agent_dir / "bad.md"
    bad.write_bytes(b"\xff\xfe invalid utf8 \x80")
    (tmp_path / "task-alpha-22.json").write_text(json.dumps({
        "issue": 22, "target": "alpha", "stage": "spec",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-22", "title": "Non-UTF8 answers task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "parked",
        "operator_request": {"kind": "answers", "path": ".agent/bad.md"},
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/22/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "answers"
    assert body["content"]["kind"] == "unavailable"
    assert "reason" in body["content"]


def test_answers_out_of_worktree_path_returns_200_unavailable(tmp_path):
    """Slice 7 (deliberate slice-5 contract update): out-of-worktree answers path was 404;
    under the new contract the request stays visible as unavailable (no bytes served)."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    wt.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    # Store a relative path that resolves outside the worktree (e.g. ../../outside.md)
    (tmp_path / "task-alpha-23.json").write_text(json.dumps({
        "issue": 23, "target": "alpha", "stage": "spec",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-23", "title": "Escaping path task",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "parked",
        "operator_request": {"kind": "answers", "path": "../../outside.md"},
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/23/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "answers"
    assert body["content"]["kind"] == "unavailable"
    # Containment check still fires: no bytes from outside the worktree are served
    assert "reason" in body["content"]


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


# ---------------------------------------------------------------------------
# Slice 14: spec content owned by spec_path; legacy backfill in _read
# ---------------------------------------------------------------------------

def test_spec_approval_reads_from_spec_path_not_artifact(tmp_path):
    """Slice 14: endpoint must resolve spec content from spec_path, ignoring artifact."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    spec = wt / "docs" / "specs" / "design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Real spec\n\nContent from spec_path.")
    (tmp_path / "task-alpha-30.json").write_text(json.dumps({
        "issue": 30, "target": "alpha", "stage": "awaiting-spec-review",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-30", "title": "Spec path test",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "awaiting-review",
        "spec_path": "docs/specs/design.md",   # worktree-relative — endpoint resolves this
        "artifact": "",                         # empty decoy — endpoint must NOT use this
        "operator_request": {"kind": "spec-approval"},
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/30/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "kind": "spec-approval",
        "content": {
            "kind": "readable",
            "path": "docs/specs/design.md",
            "media_type": "text/markdown",
            "text": "# Real spec\n\nContent from spec_path.",
        },
    }


def test_legacy_gate_record_with_backfilled_spec_path_serves_content(tmp_path):
    """Slice 14: legacy record (absolute artifact, no spec_path, no operator_request)
    loads with spec_path backfilled so endpoint resolves and serves the spec."""
    cfg = make_config(tmp_path)
    wt = tmp_path / "worktree"
    spec = wt / "docs" / "specs" / "login.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Login spec\n\nBody.")
    (tmp_path / "task-alpha-32.json").write_text(json.dumps({
        "issue": 32, "target": "alpha", "stage": "awaiting-spec-review",
        "slot": -1, "worktree": str(wt),
        "branch": "agent/task-32", "title": "Legacy gate",
        "updated_at": "2026-09-08T10:00:00+00:00",
        "park": "awaiting-review",
        "artifact": str(spec),   # absolute path — no spec_path, no operator_request
    }))
    sources = Sources(cfg, sessions=None, github=None)
    with TestClient(create_app(cfg, sources)) as client:
        response = client.get("/api/task/alpha/32/request", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "spec-approval"
    assert body["content"]["kind"] == "readable"
    assert body["content"]["path"] == "docs/specs/login.md"
    assert body["content"]["text"] == "# Login spec\n\nBody."
