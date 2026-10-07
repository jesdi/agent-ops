"""Acceptance tests for ticket 03 of the review page: an `answers` intent
becomes the answers file and resumes the session.

Spec: specs/2026-10-07-review-page/spec.md, requirements 8 and 9 and the
scenarios from "the dispatcher writes the draft file" to "'Approve' resumes the
plan session as approval", "the worktree is gone" and "an intent for a task
with no request is dropped". Black-box: intents are written with
intents.write_intent and drained by main._apply_intents (one pass); the result
is read from the worktree file, the saved task, the queued session messages and
the event log.
"""
import hashlib
import json
import os
import shutil
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher import eventlog, intents, messages
from dispatcher.state import (NO_SLOT, PARK_HUMAN, PARK_REVIEW, PARK_WAKE,
                              AnswersRequest, PlanApprovalRequest, Stage, load,
                              save)

from tests.test_main import (FakeSessions, cfg, deps, make_task, patch_usage,
                             patch_workspace)

ISSUE = 412
TARGET = "portfolio_eval"
REV = "rev-1"
PLAN_FILE = ".agent/review-answers.json"
SPEC_FILE = ".agent/questionnaire-answers.json"
CHANGES_MSG = f"Answers in {PLAN_FILE} (changes). Apply them as feedback."
APPROVE_MSG = f"Answers in {PLAN_FILE} (approve). Approved."


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return cfg(tmp_path)


def _gate(c, park=PARK_REVIEW, revision=REV):
    """Issue 412 at the plan review gate; `park` "" = not parked yet."""
    return make_task(c, issue=ISSUE, stage=Stage.AWAITING_PLAN_REVIEW,
                     slot=NO_SLOT if park else 0, park=park,
                     operator_request=PlanApprovalRequest(
                         path=".agent/review.html", fingerprint=revision))


def _intent(c, answers, submit=None, revision=REV, ms=1):
    """Write an answers intent; return its created_at."""
    p = intents.write_intent(
        c.state_dir, "answers", TARGET, ISSUE,
        {"answers": answers, "submit": submit, "revision": revision},
        actor="jesdi", epoch_ms=ms)
    return json.loads(p.read_text())["created_at"]


def _drain(c):
    main._apply_intents(c, deps(sess=FakeSessions(alive={ISSUE})))


def _file(wt, name=PLAN_FILE):
    p = wt / name
    return json.loads(p.read_text()) if p.exists() else None


def _events(c, name):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == name]


def _texts(c):
    return [m.text for m in messages.undelivered(c.state_dir, TARGET, ISSUE)]


def _task(c):
    return load(c.state_dir, TARGET, ISSUE)


def _dropped(c, reason):
    (e,) = _events(c, "intent-dropped")
    assert reason in e["detail"]
    assert not _events(c, "intent-applied")


def test_draft_writes_the_file_and_wakes_nothing(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"})

    _drain(c)

    assert _file(wt) == {"v": 1, "stage": "plan", "submitted": None,
                         "submitted_at": None, "actor": "jesdi", "revision": REV,
                         "answers": {"format": "a"}}
    assert intents.list_intents(c.state_dir) == []
    (e,) = _events(c, "intent-applied")
    assert e["issue"] == ISSUE
    assert _texts(c) == []
    assert _task(c).park == PARK_REVIEW


def test_two_drafts_leave_the_newer_answers_written_once(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, ms=1)
    _intent(c, {"format": "b"}, ms=3)
    writes = []
    real = os.replace

    def counting(src, dst, *a, **kw):
        writes.append(dst)
        return real(src, dst, *a, **kw)
    monkeypatch.setattr(os, "replace", counting)

    _drain(c)

    assert _file(wt)["answers"] == {"format": "b"}
    assert writes.count("review-answers.json") == 1
    assert intents.list_intents(c.state_dir) == []


def test_a_submission_beats_a_later_draft_in_one_pass(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    at = _intent(c, {"format": "a"}, submit="changes", ms=1)
    _intent(c, {"format": "b"}, ms=3)

    _drain(c)

    f = _file(wt)
    assert f["answers"] == {"format": "a"}
    assert f["submitted"] == "changes"
    assert f["submitted_at"] == at
    assert _texts(c) == [CHANGES_MSG]
    assert intents.list_intents(c.state_dir) == []


def test_changes_on_a_parked_gate_task_wakes_with_the_feedback_message(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    at = _intent(c, {"format": "a"}, submit="changes")

    _drain(c)

    f = _file(wt)
    assert (f["submitted"], f["submitted_at"]) == ("changes", at)
    assert _texts(c) == [CHANGES_MSG]
    assert _task(c).park == PARK_WAKE


def test_approve_wakes_and_parks_a_gate_task_that_has_not_parked(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c, park="")
    _intent(c, {"format": "a"}, submit="approve")
    sess = FakeSessions(alive={ISSUE})

    main._apply_intents(c, deps(sess=sess))

    assert _file(wt)["submitted"] == "approve"
    assert _texts(c) == [APPROVE_MSG]
    t = _task(c)
    assert t.park == PARK_WAKE
    assert ISSUE in sess.ended


def test_a_parked_answers_request_writes_the_questionnaire_file(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC)
    page = b"<p>questions</p>"
    (wt / ".agent" / "questionnaire.html").write_bytes(page)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "q",
         "artifact": ".agent/questionnaire.html"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={ISSUE})))
    assert _task(c).park == PARK_HUMAN
    _intent(c, {"format": "a"}, revision=hashlib.sha256(page).hexdigest())

    _drain(c)

    f = _file(wt, SPEC_FILE)
    assert f["stage"] == "spec" and f["answers"] == {"format": "a"}
    assert _file(wt) is None


def test_a_stale_revision_writes_nothing(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="approve", revision="rev-0")

    _drain(c)

    assert _file(wt) is None
    assert _texts(c) == []
    _dropped(c, "stale revision")


def test_an_intent_while_the_session_works_on_the_feedback_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="changes", ms=1)
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed, "the changes wake resumes the plan session"
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "working", "note": "applying feedback"}))
    main.run_pass(c, deps(sess=sess))
    (wt / PLAN_FILE).unlink()
    before = len(_events(c, "intent-applied"))
    _intent(c, {"format": "b"}, submit="approve", ms=5)

    main.run_pass(c, deps(sess=sess))

    assert _file(wt) is None
    (e,) = _events(c, "intent-dropped")
    assert "no open request" in e["detail"]   # the rework cleared the request
    assert len(_events(c, "intent-applied")) == before


def test_a_draft_after_a_submission_leaves_the_file_unchanged(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="approve", ms=1)
    _drain(c)
    saved = (wt / PLAN_FILE).read_bytes()
    assert json.loads(saved)["submitted"] == "approve"
    c_events = len(_events(c, "intent-dropped"))
    _intent(c, {"format": "b"}, ms=5)

    _drain(c)

    assert (wt / PLAN_FILE).read_bytes() == saved
    assert len(_events(c, "intent-dropped")) == c_events + 1
    assert "already submitted" in _events(c, "intent-dropped")[-1]["detail"]


def test_an_intent_for_a_task_without_a_request_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    _intent(c, {"format": "a"})

    _drain(c)

    assert _file(wt) is None
    _dropped(c, "no open request")


def test_a_gone_worktree_fails_the_intent_and_creates_nothing(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    shutil.rmtree(wt)
    _intent(c, {"format": "a"})

    _drain(c)

    assert not wt.exists()
    assert intents.list_intents(c.state_dir) == []
    assert not _events(c, "intent-applied")
    assert "failed" in capsys.readouterr().err


def test_an_answers_request_revision_is_the_sha256_of_the_page(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC)
    page = wt / ".agent" / "questionnaire.html"
    revisions = []
    for body in ("<p>one</p>", "<p>two</p>"):
        page.write_text(body)
        (wt / ".agent" / "stage.json").write_text(json.dumps(
            {"stage": "spec", "status": "awaiting-answers", "note": "q",
             "artifact": ".agent/questionnaire.html"}))
        main.run_pass(c, deps(sess=FakeSessions(alive={ISSUE})))
        req = _task(c).operator_request
        assert req.fingerprint == hashlib.sha256(body.encode()).hexdigest()
        revisions.append(req.fingerprint)
        save(c.state_dir, dc_replace(_task(c), park="", slot=0, operator_request=None))
    assert revisions[0] != revisions[1]
