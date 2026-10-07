"""Edge cases of the answers intent the acceptance file does not reach."""

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import answers, eventlog, intents, messages
from dispatcher.artifacts import REVIEW_PAGE_MAX_BYTES, page_revision
from dispatcher.state import (NO_SLOT, PARK_HUMAN, PARK_REVIEW, PARK_WAKE,
                              AnswersRequest, PlanApprovalRequest, Stage, save,
                              task_key)

from tests.test_answers_intent_acceptance import (ISSUE, PLAN_FILE, REV, TARGET,
                                                  _drain, _events, _file,
                                                  _gate, _intent, _setup,
                                                  _task, _texts)
from tests.test_main import make_task


def test_an_unknown_submit_value_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="merge")

    _drain(c)

    assert _file(wt) is None
    (e,) = _events(c, "intent-dropped")
    assert "unknown submit" in e["detail"]


def test_a_garbage_file_on_disk_does_not_block_a_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    (wt / PLAN_FILE).write_text("not json")
    _intent(c, {"format": "a"})

    _drain(c)

    assert _file(wt)["answers"] == {"format": "a"}
    assert [e["event"] for e in eventlog.read_tail(c.state_dir)] == ["intent-applied"]


def test_a_request_without_a_revision_matches_no_intent(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c, revision="")
    _intent(c, {"format": "a"}, revision="")

    _drain(c)

    assert _file(wt) is None
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]


def test_an_intent_for_an_unknown_task_is_dropped(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _intent(c, {"format": "a"})

    _drain(c)

    (e,) = _events(c, "intent-dropped")
    assert "no open request" in e["detail"]


def test_a_submission_while_the_wake_is_queued_is_session_busy(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="changes", ms=1)
    _drain(c)
    _intent(c, {"format": "b"}, submit="approve", ms=5)

    _drain(c)

    assert _file(wt)["submitted"] == "changes"
    (e,) = _events(c, "intent-dropped")
    assert "session busy" in e["detail"]


def test_a_stale_approval_does_not_cost_a_current_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, ms=1)
    _intent(c, {"format": "old"}, submit="approve", revision="rev-0", ms=5)

    _drain(c)

    f = _file(wt)
    assert (f["answers"], f["submitted"]) == ({"format": "a"}, None)
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]
    assert len(_events(c, "intent-applied")) == 1
    assert _texts(c) == []
    assert intents.list_intents(c.state_dir) == []


def test_a_missing_agent_dir_fails_the_intent_and_creates_nothing(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    shutil.rmtree(wt / ".agent")
    _intent(c, {"format": "a"})

    _drain(c)

    assert not (wt / ".agent").exists()
    assert intents.list_intents(c.state_dir) == []
    assert not _events(c, "intent-applied")
    assert "failed" in capsys.readouterr().err


def test_a_submission_resets_the_loop_counters(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    make_task(c, issue=ISSUE, stage=Stage.AWAITING_PLAN_REVIEW, slot=NO_SLOT,
              park=PARK_REVIEW, review_rounds=1, gate_rounds=2, e2e_rounds=1,
              ci_rounds=3, operator_request=PlanApprovalRequest(
                  path=".agent/review.html", fingerprint=REV))
    _intent(c, {"format": "a"}, submit="changes")

    _drain(c)

    t = _task(c)
    assert t.park == PARK_WAKE
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0)


def test_an_agent_path_that_is_a_file_fails_the_intent(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    shutil.rmtree(wt / ".agent")
    (wt / ".agent").write_text("x")
    _intent(c, {"format": "a"})

    _drain(c)

    assert (wt / ".agent").read_text() == "x"
    assert not _events(c, "intent-applied")
    assert "not a directory" in capsys.readouterr().err


def test_a_draft_on_a_new_revision_is_not_blocked_by_an_old_submission(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, submit="changes", ms=1)
    _drain(c)
    t = _task(c)
    save(c.state_dir, replace(t, park=PARK_REVIEW, operator_request=replace(
        t.operator_request, fingerprint="rev-2")))
    _intent(c, {"format": "b"}, revision="rev-2", ms=5)

    _drain(c)

    f = _file(wt)
    assert (f["answers"], f["submitted"], f["revision"]) == ({"format": "b"}, None, "rev-2")
    assert not _events(c, "intent-dropped")


def test_a_corrupt_task_file_fails_only_its_own_answers_intent(tmp_path, monkeypatch, capsys):
    c = _setup(tmp_path, monkeypatch)
    _gate(c)
    (Path(c.state_dir) / f"task-{task_key(TARGET, ISSUE)}.json").write_text("{not json")
    make_task(c, issue=7, park=PARK_HUMAN, slot=NO_SLOT)
    _intent(c, {"format": "a"}, ms=1)
    intents.write_intent(c.state_dir, "reply", TARGET, 7, {"text": "go"},
                         actor="jesdi", epoch_ms=2)

    _drain(c)

    assert intents.list_intents(c.state_dir) == []
    (e,) = _events(c, "intent-applied")
    assert (e["issue"], e["detail"]) == (7, "reply")
    assert [m.text for m in messages.undelivered(c.state_dir, TARGET, 7)] == ["go"]
    assert "failed" in capsys.readouterr().err


def test_a_text_answer_without_revision_blocks_a_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    text_answer = json.dumps({"v": 1, "stage": "plan", "submitted": "approve",
                              "submitted_at": "2026-10-07T10:00:05+00:00",
                              "actor": "text", "answers": {"format": "a"}})
    (wt / PLAN_FILE).write_text(text_answer)
    _intent(c, {"format": "b"})

    _drain(c)

    assert (wt / PLAN_FILE).read_text() == text_answer
    (e,) = _events(c, "intent-dropped")
    assert "already submitted" in e["detail"]


def test_a_garbage_submit_does_not_beat_a_valid_draft(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _gate(c)
    _intent(c, {"format": "a"}, ms=1)
    _intent(c, {"format": "x"}, submit="merge", ms=5)

    _drain(c)

    assert _file(wt)["answers"] == {"format": "a"}
    (e,) = _events(c, "intent-dropped")
    assert "unknown submit" in e["detail"]
    assert len(_events(c, "intent-applied")) == 1


FIXTURE = Path(__file__).parent / "fixtures" / "review-page.html"


def _real_gate(c):
    """Issue 412 at the gate, its request armed for the page on disk."""
    wt = make_task(c, issue=ISSUE)
    (wt / ".agent" / "review.html").write_bytes(FIXTURE.read_bytes())
    task = _task(c)
    req = main._plan_approval(task, ".agent/review.html")
    assert req.fingerprint
    save(c.state_dir, replace(task, stage=Stage.AWAITING_PLAN_REVIEW, slot=NO_SLOT,
                              park=PARK_REVIEW, operator_request=req))
    return wt, req.fingerprint


def test_an_intent_for_a_page_rewritten_since_the_pass_is_stale(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, rev = _real_gate(c)
    with (wt / ".agent" / "review.html").open("a") as f:
        f.write("<!-- rewritten -->")
    _intent(c, {"format": "a"}, submit="approve", revision=rev)

    _drain(c)

    assert _file(wt) is None
    assert _texts(c) == []
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]


def test_an_intent_for_the_page_on_disk_is_applied(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, rev = _real_gate(c)
    _intent(c, {"format": "a"}, submit="approve", revision=rev)

    _drain(c)

    assert _file(wt)["submitted"] == "approve"


def test_a_questionnaire_rewritten_since_the_pass_is_stale(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, park=PARK_HUMAN, slot=NO_SLOT,
                   operator_request=AnswersRequest(
                       path=".agent/questionnaire.html",
                       fingerprint=hashlib.sha256(b"<p>one</p>").hexdigest()))
    (wt / ".agent" / "questionnaire.html").write_text("<p>two</p>")
    _intent(c, {"format": "a"}, revision=hashlib.sha256(b"<p>one</p>").hexdigest())

    _drain(c)

    assert _file(wt, ".agent/questionnaire-answers.json") is None
    (e,) = _events(c, "intent-dropped")
    assert "stale revision" in e["detail"]


def test_a_new_round_removes_the_text_answer_of_the_last_one(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE)
    (wt / ".agent" / "review.html").write_bytes(FIXTURE.read_bytes())
    (wt / PLAN_FILE).write_text(json.dumps(
        {"v": 1, "stage": "plan", "submitted": "changes", "actor": "text",
         "answers": {"format": "a"}}))

    req = main._plan_approval(_task(c), ".agent/review.html")

    assert req.fingerprint and _file(wt) is None


@pytest.mark.parametrize("doc, kept", [
    ({"submitted": None, "revision": "r2"}, True),        # a draft of this round
    ({"submitted": None, "revision": "r1"}, False),       # a draft of the last one
    ({"submitted": "changes", "revision": "r2"}, False),  # read by the session it woke
    ({"submitted": "approve", "revision": None}, False),  # a text answer
    ("not json", False),
])
def test_discard_stale_keeps_only_a_draft_of_the_armed_revision(tmp_path, doc, kept):
    (tmp_path / ".agent").mkdir()
    f = tmp_path / ".agent" / "review-answers.json"
    f.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    answers.discard_stale(tmp_path, "plan-approval", "r2")
    assert f.exists() is kept


def test_discard_stale_tolerates_an_agent_dir_that_is_a_symlink(tmp_path, capsys):
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "review-answers.json").write_text("{}")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".agent").symlink_to(tmp_path / "elsewhere")
    answers.discard_stale(wt, "plan-approval", "r2")
    assert (tmp_path / "elsewhere" / "review-answers.json").exists()
    assert "not removed" in capsys.readouterr().err


def test_page_revision_is_the_digest_of_the_bytes_or_the_problem(tmp_path):
    p = tmp_path / "q.html"
    p.write_bytes(b"<p>q</p>")
    assert page_revision(p) == (hashlib.sha256(b"<p>q</p>").hexdigest(), "")
    assert page_revision(tmp_path / "missing.html") == ("", "review page missing")
    p.write_bytes(b"x" * (REVIEW_PAGE_MAX_BYTES + 1))
    assert page_revision(p)[0] == ""
