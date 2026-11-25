"""Acceptance tests for #282 ticket 03: the collector captures a session's text
answer and never undoes a capture.

Spec: specs/2026-11-24-answered-page-artifact/spec.md, requirements 6, 7 and 8.
Black-box: the session writes the answers file itself (`"actor": "text"`, no
revision); the collector runs with `task_artifacts.collect(..., publish=False)`;
the artifact is read through the console API
(`GET /api/task/{repository}/{issue}/artifacts/{id}`).
"""
import json

from dispatcher.state import Stage
from tests.dispatchfakes import make_task
from tests.test_answered_page_capture_acceptance import (
    ISSUE, NEXT_PAGE, QUESTIONNAIRE, QUESTIONNAIRE_FILE, REVIEW, REVIEW_PAGE,
    _answered, _answered_page, _collect, _get, _listing, _Page, _questionnaire, _setup)

REVIEW_FILE = ".agent/review-answers.json"


def _text_answer(wt, path, answers_, submitted="changes"):
    """The session writes the answers file itself from the operator's text reply."""
    (wt / path).write_text(json.dumps(
        {"v": 1, "stage": "spec", "submitted": submitted, "submitted_at": None,
         "actor": "text", "revision": None, "answers": answers_}))


def test_a_text_answer_on_the_questionnaire_is_captured(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _questionnaire(c)
    assert "questionnaire-answers" not in [i["id"] for i in _listing(c)]
    _text_answer(wt, QUESTIONNAIRE_FILE, {"format": "csv"})
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert "Which export format?" in text
    assert page.checked("format") == ["csv"]


def test_a_text_review_answer_with_no_open_request_uses_the_review_page(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.PLAN)
    (wt / REVIEW).write_bytes(REVIEW_PAGE)
    _text_answer(wt, REVIEW_FILE, {"format": "b"})
    _collect(c)

    text, page = _answered_page(c, "review-answers")
    assert "Which layout?" in text
    assert page.checked("format") == ["b"]


def test_a_text_review_answer_under_an_open_questionnaire_uses_the_review_page(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _questionnaire(c)
    (wt / REVIEW).write_bytes(REVIEW_PAGE)
    _text_answer(wt, REVIEW_FILE, {"format": "b"})
    _collect(c)

    text, page = _answered_page(c, "review-answers")
    assert "Which layout?" in text
    assert "Which export format?" not in text
    assert page.checked("format") == ["b"]


def test_a_drained_answers_file_is_not_rebuilt_with_the_rewritten_page(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    captured = _get(c, "questionnaire-answers").content
    assert _Page(captured.decode()).checked("format") == ["parquet"]
    (wt / QUESTIONNAIRE).write_bytes(NEXT_PAGE)
    # The same collection captures a text answer of the other kind: the
    # collector does capture, and still leaves the drained page alone.
    (wt / REVIEW).write_bytes(REVIEW_PAGE)
    _text_answer(wt, REVIEW_FILE, {"format": "b"})
    _collect(c)

    assert _get(c, "questionnaire-answers").content == captured
    _, review_page = _answered_page(c, "review-answers")
    assert review_page.checked("format") == ["b"]


def test_a_draft_text_answer_produces_no_answers_artifact(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _questionnaire(c)
    _text_answer(wt, QUESTIONNAIRE_FILE, {"format": "csv"}, submitted=None)
    # A submitted text answer of the other kind in the same collection.
    (wt / REVIEW).write_bytes(REVIEW_PAGE)
    _text_answer(wt, REVIEW_FILE, {"format": "b"})
    _collect(c)

    ids = [i["id"] for i in _listing(c)]
    assert "review-answers" in ids
    assert "questionnaire-answers" not in ids


def test_session_manifest_entries_do_not_replace_captured_pages(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    (wt / REVIEW).write_bytes(REVIEW_PAGE)
    _text_answer(wt, REVIEW_FILE, {"format": "b"})
    (wt / ".agent/artifacts.json").write_text(json.dumps([
        {"id": "questionnaire-answers", "name": "Questionnaire answers",
         "path": QUESTIONNAIRE_FILE},
        {"id": "review-answers", "name": "Review answers", "path": REVIEW_FILE}]))
    _collect(c)
    _collect(c)

    _, questionnaire = _answered_page(c, "questionnaire-answers")
    assert questionnaire.checked("format") == ["parquet"]
    _, review = _answered_page(c, "review-answers")
    assert review.checked("format") == ["b"]
    ids = [i["id"] for i in _listing(c)]
    assert ids.count("questionnaire-answers") == 1 and ids.count("review-answers") == 1
