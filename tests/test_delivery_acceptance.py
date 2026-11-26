"""Acceptance tests for ticket 03 of the review send delivery: the console reads
whether the task's latest submission was delivered.

Spec: specs/2026-11-24-review-send-delivery/spec.md, requirements 4 and 5,
scenarios 8 to 12. Black-box: intents are written with intents.write_intent
(their created_at set to the submission time of the scenario), drained by one
operator_intents._apply_intents pass, and the result is read from
GET /api/task/{repository}/{issue}/delivery.
"""
import hashlib
import json
from dataclasses import replace as dc_replace

from fastapi.testclient import TestClient

from dispatcher import eventlog, intents, operator_intents
from dispatcher.state import NO_SLOT, PlanApprovalRequest, Stage, load, save
from tests.dispatchfakes import FakeSessions, cfg, deps, make_task, patch_usage, patch_workspace
from tests.statuses import RUNNING, human, review
from tests.webfakes import HEADERS
from web.app import create_app
from web.sources import Sources

ISSUE = 412
REPOSITORY = "portfolio_eval"
R1 = "r1"
ANSWERS = ".agent/review-answers.json"
AT_10_00 = "2026-11-24T10:00:00+00:00"
AT_10_01 = "2026-11-24T10:01:00+00:00"
AT_10_02 = "2026-11-24T10:02:00+00:00"


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return cfg(tmp_path)


def _gate(c, revision=R1):
    """Issue 412 parked at the plan review gate on `revision`."""
    request = PlanApprovalRequest(path=".agent/review.html", fingerprint=revision)
    return make_task(c, issue=ISSUE, stage=Stage.PLAN, slot=NO_SLOT, status=review(request))


def _intent(c, payload, at, action="answers", repository=REPOSITORY):
    """An intent of the console, made at `at`."""
    p = intents.write_intent(c.state_dir, action, repository, ISSUE, payload,
                             actor="jesdi", epoch_ms=1)
    rec = json.loads(p.read_text())
    rec["created_at"] = at
    p.write_text(json.dumps(rec))


def _submission(c, at, revision=R1, submit="changes"):
    _intent(c, {"answers": {"format": "a"}, "submit": submit, "revision": revision}, at)


def _drain(c):
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={ISSUE})))


def _drop(c, at, revision="r0", submit="changes"):
    """A submission made at `at` on a revision the gate no longer shows,
    dropped by one drain."""
    _submission(c, at, revision=revision, submit=submit)
    _drain(c)
    assert [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "intent-dropped"]
    assert intents.list_intents(c.state_dir) == []


def _answers_file(wt, submitted_at, submitted="changes", revision=R1):
    (wt / ANSWERS).write_text(json.dumps(
        {"v": 1, "stage": "plan", "submitted": submitted, "submitted_at": submitted_at,
         "actor": "jesdi", "revision": revision, "answers": {"format": "a"}}))


def _get(c, route):
    with TestClient(create_app(c, Sources(c.state_dir, sessions=None, github=None))) as client:
        r = client.get(f"/api/task/{REPOSITORY}/{ISSUE}/{route}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _delivery(c):
    return _get(c, "delivery")


def test_a_pending_submission_is_queued(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gate(c)
    _submission(c, AT_10_00)

    assert _delivery(c) == {"state": "queued", "reason": "", "submit": "changes",
                            "revision": R1}


def test_a_submitted_answers_file_is_delivered(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _answers_file(wt, AT_10_00)

    assert _delivery(c) == {"state": "delivered", "reason": "", "submit": "changes",
                            "revision": R1}


def test_a_dropped_submission_is_dropped_with_its_reason(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gate(c, revision="r2")
    _drop(c, AT_10_01, revision=R1)

    assert _delivery(c) == {"state": "dropped", "reason": "stale revision",
                            "submit": "changes", "revision": R1}


def test_an_answers_file_submitted_after_the_drop_is_delivered(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c, revision="r2")
    _drop(c, AT_10_01, revision=R1)
    _answers_file(wt, AT_10_02, revision="r2")

    assert _delivery(c) == {"state": "delivered", "reason": "", "submit": "changes",
                            "revision": "r2"}


def test_submission_time_decides_not_the_time_the_drop_was_written(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c, revision="r2")
    _answers_file(wt, AT_10_02, revision="r2")
    _drop(c, AT_10_00, revision=R1)   # written after the file, made before it

    assert _delivery(c) == {"state": "delivered", "reason": "", "submit": "changes",
                            "revision": "r2"}


def test_a_dropped_draft_is_no_delivery(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gate(c, revision="r2")
    _drop(c, AT_10_01, revision=R1, submit=None)

    assert _delivery(c) is None


def test_a_dropped_resume_is_no_delivery(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT, slot=NO_SLOT, status=human())
    _intent(c, {"model": "no-such-model"}, AT_10_01, action="resume")
    _drain(c)
    (e,) = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "intent-dropped"]
    assert e["issue"] == ISSUE

    assert _delivery(c) is None


def test_a_drop_of_another_repository_with_the_same_issue_is_no_delivery(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gate(c)
    _intent(c, {"answers": {"format": "a"}, "submit": "changes", "revision": R1},
            AT_10_01, repository="other_repo")
    _drain(c)
    (e,) = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "intent-dropped"]
    assert e["issue"] == ISSUE

    assert _delivery(c) is None


def test_a_draft_only_answers_file_is_no_delivery(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _answers_file(wt, None, submitted=None)

    assert _delivery(c) is None


def test_the_delivery_outlives_the_request_it_closed(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gate(c)
    _submission(c, AT_10_00)

    _drain(c)
    t = load(c.state_dir, REPOSITORY, ISSUE)
    save(c.state_dir, dc_replace(t, status=RUNNING, slot=0))   # the woken session runs

    assert _get(c, "request") is None
    assert _delivery(c) == {"state": "delivered", "reason": "", "submit": "changes",
                            "revision": R1}


def test_a_submission_held_while_the_session_runs_is_queued(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    page = b'<meta name="agent-ops-review" content="1"><p>questions</p>'
    revision = hashlib.sha256(page).hexdigest()
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, slot=0, status=RUNNING)
    (wt / ".agent" / "questionnaire.html").write_bytes(page)
    _submission(c, AT_10_00, revision=revision)

    _drain(c)

    assert len(intents.list_intents(c.state_dir)) == 1   # held, not drained
    assert _get(c, "request") is None
    assert _delivery(c) == {"state": "queued", "reason": "", "submit": "changes",
                            "revision": revision}
