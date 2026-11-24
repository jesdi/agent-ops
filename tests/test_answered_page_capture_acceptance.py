"""Acceptance tests for #282 ticket 01: a drained submission is archived as the
answered page.

Spec: specs/2026-11-24-answered-page-artifact/spec.md, requirements 1, 2, 4,
5, 7, 8, 9 and 10. Black-box: the operator's answers are an `answers` intent
drained by `operator_intents._apply_intents`; the collector runs with
`task_artifacts.collect(..., publish=False)`; the artifact is read through the
console API (`GET /api/task/{repository}/{issue}/artifacts[/{id}]`), as the
Artifacts panel does; retention runs through `task_artifacts.cleanup`.
"""
import hashlib
import json
import logging
import os
import pathlib
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser

from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher import answers, intents, messages, operator_intents, task_artifacts
from dispatcher.state import NO_SLOT, AnswersRequest, PlanApprovalRequest, Stage, archive_root, load, save
from dispatcher.status import Kind
from tests.dispatchfakes import (FakeSessions, armed_request, cfg, deps, make_task,
                                 patch_usage, patch_workspace)
from tests.statuses import DONE, review
from tests.statuses import answers as parked_answers
from tests.webfakes import HEADERS, FakeSources, make_config, make_repository
from web.app import create_app
from web.artifacts import SANDBOX

ISSUE = 412
REPOSITORY = "portfolio_eval"
QUESTIONNAIRE = ".agent/questionnaire.html"
QUESTIONNAIRE_FILE = ".agent/questionnaire-answers.json"
REVIEW = ".agent/review.html"
PAGE = b"""<!doctype html><html><head><meta name="agent-ops-review" content="1"></head><body>
<div class="q" data-q="format"><h2>Which export format?</h2>
<label><input type="radio" name="format" value="csv"> CSV</label>
<label><input type="radio" name="format" value="parquet"> Parquet</label>
<textarea id="note-format"></textarea></div>
<select id="extra"><option>x</option></select>
<button id="send">Send answers</button>
<script>window.page = 1;</script>
</body></html>"""
NEXT_PAGE = PAGE.replace(b"export", b"import")
REVIEW_PAGE = b"""<!doctype html><html><head><meta name="agent-ops-review" content="1"></head><body>
<div class="q" data-q="format"><h2>Which layout?</h2>
<label><input type="radio" name="format" value="a"> A</label>
<label><input type="radio" name="format" value="b"> B</label>
<textarea id="note-format"></textarea></div>
<div class="track">
<label><input type="radio" name="track" value="standard" checked>standard</label>
<label><input type="radio" name="track" value="critical">critical</label>
</div>
<button id="approve">Approve</button>
<script>window.review = 1;</script>
</body></html>"""
ANSWERS = {"format": "parquet", "format.note": "keep it columnar"}
TERMINAL = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, tzinfo=timezone.utc)


# --- the console's view of the archive ---------------------------------------

class _Page(HTMLParser):
    """The form controls, textarea texts and scripts of an HTML document."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.controls, self.textareas, self.scripts = [], {}, 0
        self._textarea = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("input", "textarea", "select", "button"):
            self.controls.append((tag, attrs))
        if tag == "textarea":
            self._textarea = attrs.get("id", "")
            self.textareas[self._textarea] = ""
        if tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea is not None:
            self.textareas[self._textarea] += data

    def checked(self, name):
        return [a.get("value") for t, a in self.controls
                if t == "input" and a.get("type") == "radio" and a.get("name") == name
                and "checked" in a]


def _client(c, task=None):
    sources = FakeSources(c.state_dir)
    sources.tasks_list = [task or load(c.state_dir, REPOSITORY, ISSUE)]
    config = make_config(c.state_dir, [make_repository(REPOSITORY, "jesdi/portfolio_eval")])
    return TestClient(create_app(config, sources))


def _get(c, artifact_id, task=None):
    url = f"/api/task/{REPOSITORY}/{ISSUE}/artifacts/{artifact_id}"
    return _client(c, task).get(url, headers=HEADERS)


def _listing(c, task=None):
    body = _client(c, task).get(f"/api/task/{REPOSITORY}/{ISSUE}/artifacts", headers=HEADERS).json()
    return body["items"]


def _answered_page(c, artifact_id):
    response = _get(c, artifact_id)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert not response.text.lstrip().startswith("{"), "the answers open as raw JSON"
    return response.text, _Page(response.text)


def _collect(c, now=None):
    task_artifacts.collect(c.state_dir, load(c.state_dir, REPOSITORY, ISSUE), "",
                           publish=False, now=now)


# --- the dispatcher side ------------------------------------------------------

def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return cfg(tmp_path)


def _park_questionnaire(c, wt, page):
    """The session reports its questionnaire; a pass parks the task on it."""
    (wt / QUESTIONNAIRE).write_bytes(page)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "q",
         "artifact": QUESTIONNAIRE}))
    main.run_pass(c, deps(sess=FakeSessions(alive={ISSUE})))
    assert load(c.state_dir, REPOSITORY, ISSUE).status.kind is Kind.PARKED_ANSWERS


def _next_round(c, wt, page=NEXT_PAGE):
    """The woken session rewrites its questionnaire and parks on it again:
    the next revision is armed and the stale answers file discarded."""
    (wt / QUESTIONNAIRE).write_bytes(page)
    revision = hashlib.sha256(page).hexdigest()
    answers.discard_stale(wt, "answers", revision)
    task = load(c.state_dir, REPOSITORY, ISSUE)
    save(c.state_dir, replace(task, slot=NO_SLOT, status=parked_answers(
        AnswersRequest(QUESTIONNAIRE, fingerprint=revision))))


def _questionnaire(c, page=PAGE):
    """Issue 412 at stage spec, parked on its questionnaire."""
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC)
    _park_questionnaire(c, wt, page)
    return wt


def _drain(c, answers_, submit, page=PAGE):
    intents.write_intent(c.state_dir, "answers", REPOSITORY, ISSUE,
                         {"answers": answers_, "submit": submit,
                          "revision": hashlib.sha256(page).hexdigest()},
                         actor="jesdi", epoch_ms=1)
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={ISSUE})))


def _answered(tmp_path, monkeypatch, answers_=ANSWERS):
    """The reproduction: 412's questionnaire answered with "Send answers"."""
    c = _setup(tmp_path, monkeypatch)
    wt = _questionnaire(c)
    _drain(c, answers_, "changes")
    assert (wt / QUESTIONNAIRE_FILE).exists()
    return c, wt


def _plan_gate(c, page=REVIEW_PAGE):
    """Issue 412 parked at the plan review gate on its review page."""
    wt = make_task(c, issue=ISSUE, stage=Stage.PLAN, slot=NO_SLOT,
                   status=review(PlanApprovalRequest(REVIEW, fingerprint="")))
    (wt / REVIEW).write_bytes(page)
    task = load(c.state_dir, REPOSITORY, ISSUE)
    request = armed_request(task, REVIEW)
    save(c.state_dir, replace(task, status=review(request)))
    return wt, request.fingerprint


def _approve(c, answers_, revision):
    intents.write_intent(c.state_dir, "answers", REPOSITORY, ISSUE,
                         {"answers": answers_, "submit": "approve", "revision": revision},
                         actor="jesdi", epoch_ms=1)
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={ISSUE})))


# --- the answered page ------------------------------------------------------

def test_the_reproduction_opens_inline_as_the_answered_page(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch)
    _collect(c)

    response = _get(c, "questionnaire-answers")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == SANDBOX
    assert "content-disposition" not in response.headers
    assert not response.text.lstrip().startswith("{")
    page = _Page(response.text)
    assert "Which export format?" in response.text
    assert page.checked("format") == ["parquet"]
    assert page.textareas["note-format"] == "keep it columnar"


def test_the_listing_entry_is_the_captured_page(tmp_path, monkeypatch):
    before = datetime.now(timezone.utc)
    c, wt = _answered(tmp_path, monkeypatch)
    after = datetime.now(timezone.utc)
    digest = hashlib.sha256((wt / QUESTIONNAIRE_FILE).read_bytes()).hexdigest()
    _collect(c, now=LATER)   # a later collection does not restamp the capture

    [entry] = [i for i in _listing(c) if i["id"] == "questionnaire-answers"]
    assert (entry["name"], entry["path"], entry["stage"], entry["status"], entry["media_type"]) == (
        "Questionnaire answers", QUESTIONNAIRE_FILE, "spec", "local", "text/html")
    assert before <= datetime.fromisoformat(entry["updated_at"]) <= after
    [item] = [i for i in task_artifacts.read(c.state_dir, REPOSITORY, ISSUE).items
              if i.id == "questionnaire-answers"]
    assert item.digest == digest   # of the answers file, not of the HTML


def test_the_answered_page_is_served_after_the_worktree_is_deleted(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    shutil.rmtree(wt)
    _collect(c)

    _, page = _answered_page(c, "questionnaire-answers")
    assert page.checked("format") == ["parquet"]
    assert page.textareas["note-format"] == "keep it columnar"


def test_the_answered_page_is_served_after_the_worktree_becomes_a_file(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    shutil.rmtree(wt)
    wt.write_text("not a worktree")
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert "Which export format?" in text
    assert page.checked("format") == ["parquet"]


def test_the_page_as_answered_survives_the_next_round(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    (wt / QUESTIONNAIRE).write_bytes(NEXT_PAGE)
    answers.discard_stale(wt, "answers", "rev-2")
    assert not (wt / QUESTIONNAIRE_FILE).exists()
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert "Which export format?" in text
    assert "Which import format?" not in text
    assert page.checked("format") == ["parquet"]


def test_a_plan_approval_opens_as_the_answered_review_page(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _, revision = _plan_gate(c)
    _approve(c, {"format": "b", "track": "critical"}, revision)
    _collect(c)

    text, page = _answered_page(c, "review-answers")
    assert "Which layout?" in text
    assert page.checked("format") == ["b"]
    assert page.checked("track") == ["critical"]   # the default `standard` unchecked
    [entry] = [i for i in _listing(c) if i["id"] == "review-answers"]
    assert (entry["name"], entry["media_type"], entry["status"]) == (
        "Review answers", "text/html", "local")


def test_an_approval_without_a_track_checks_no_track_pill(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _, revision = _plan_gate(c)
    _approve(c, {}, revision)
    _collect(c)

    _, page = _answered_page(c, "review-answers")
    assert page.checked("track") == []
    assert page.checked("format") == []


def test_every_control_of_the_answered_page_is_disabled(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch)
    _collect(c)

    _, page = _answered_page(c, "questionnaire-answers")
    tags = {t for t, _ in page.controls}
    assert {"input", "textarea", "select", "button"} <= tags
    assert ("input", "csv") in [(t, a.get("value")) for t, a in page.controls]
    enabled = [(t, a) for t, a in page.controls if "disabled" not in a]
    assert enabled == []


def test_markup_in_a_note_shows_as_text(tmp_path, monkeypatch):
    note = "</textarea><script>alert(1)</script> & more"
    c, _ = _answered(tmp_path, monkeypatch, {"format": "parquet", "format.note": note})
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert page.textareas["note-format"] == note
    assert page.scripts == _Page(PAGE.decode()).scripts
    assert "alert(1)</script>" not in text


# --- drafts --------------------------------------------------------------------

def test_a_draft_creates_no_answers_artifact(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _questionnaire(c)
    _drain(c, {"format": "csv"}, None)
    assert (wt / QUESTIONNAIRE_FILE).exists()
    _collect(c)

    assert "questionnaire-answers" not in [i["id"] for i in _listing(c)]


def test_a_draft_after_a_submission_leaves_the_captured_page(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    _collect(c)
    captured = _get(c, "questionnaire-answers").content
    _next_round(c, wt)
    _drain(c, {"format": "csv"}, None, page=NEXT_PAGE)
    assert json.loads((wt / QUESTIONNAIRE_FILE).read_text())["answers"] == {"format": "csv"}
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert "Which export format?" in text
    assert page.checked("format") == ["parquet"]
    assert text.encode() == captured


# --- the next submission ------------------------------------------------------

def test_the_next_submission_replaces_the_page_at_the_same_url(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    _collect(c)
    _next_round(c, wt)
    _drain(c, {"format": "csv"}, "changes", page=NEXT_PAGE)
    _collect(c)

    text, page = _answered_page(c, "questionnaire-answers")
    assert "Which import format?" in text
    assert page.checked("format") == ["csv"]
    assert [i["id"] for i in _listing(c)].count("questionnaire-answers") == 1


# --- a capture that fails -------------------------------------------------------

def _queued(c):
    return [m.text for m in messages.undelivered(c.state_dir, REPOSITORY, ISSUE)]


def _fail_archive_writes(monkeypatch, c):
    """Every write into the task's archive raises, as on a full disk."""
    root = str(archive_root(c.state_dir, REPOSITORY, ISSUE))
    real_replace = os.replace
    real_bytes, real_text = pathlib.Path.write_bytes, pathlib.Path.write_text

    def inside(path):
        return os.path.abspath(os.fspath(path)).startswith(root)

    def replace_(src, dst, *a, **kw):
        if not kw.get("dst_dir_fd") and inside(dst):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *a, **kw)

    def write_bytes(self, data):
        if inside(self):
            raise OSError(28, "No space left on device")
        return real_bytes(self, data)

    def write_text(self, data, *a, **kw):
        if inside(self):
            raise OSError(28, "No space left on device")
        return real_text(self, data, *a, **kw)
    monkeypatch.setattr(os, "replace", replace_)
    monkeypatch.setattr(pathlib.Path, "write_bytes", write_bytes)
    monkeypatch.setattr(pathlib.Path, "write_text", write_text)


def _second_round(c, wt):
    """The first submission captured; the next round's page armed."""
    _collect(c)
    captured = _get(c, "questionnaire-answers").content
    assert _Page(captured.decode()).checked("format") == ["parquet"]
    _next_round(c, wt)
    messages_before = len(_queued(c))
    return captured, messages_before


def _assert_answers_kept_and_warned(c, wt, captured, messages_before, caplog):
    doc = json.loads((wt / QUESTIONNAIRE_FILE).read_text())
    assert (doc["submitted"], doc["answers"]) == ("changes", {"format": "csv"})
    assert len(_queued(c)) == messages_before + 1
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert _get(c, "questionnaire-answers").content == captured


def test_a_missing_request_page_still_records_and_wakes(tmp_path, monkeypatch, caplog):
    c, wt = _answered(tmp_path, monkeypatch)
    captured, messages_before = _second_round(c, wt)
    (wt / QUESTIONNAIRE).unlink()
    caplog.clear()
    caplog.set_level(logging.WARNING)

    _drain(c, {"format": "csv"}, "changes", page=NEXT_PAGE)

    _assert_answers_kept_and_warned(c, wt, captured, messages_before, caplog)


def test_a_failed_archive_write_still_records_and_wakes(tmp_path, monkeypatch, caplog):
    c, wt = _answered(tmp_path, monkeypatch)
    captured, messages_before = _second_round(c, wt)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    with monkeypatch.context() as m:
        _fail_archive_writes(m, c)
        _drain(c, {"format": "csv"}, "changes", page=NEXT_PAGE)

    _assert_answers_kept_and_warned(c, wt, captured, messages_before, caplog)


# --- retention ------------------------------------------------------------------

def test_a_captured_review_answers_expires_with_the_task(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _, revision = _plan_gate(c)
    _approve(c, {"format": "b", "track": "critical"}, revision)
    task = replace(load(c.state_dir, REPOSITORY, ISSUE), stage=Stage.QUEUED, status=DONE,
                   terminal_at=TERMINAL.isoformat())
    save(c.state_dir, task)
    task_artifacts.collect(c.state_dir, task, "", publish=False, now=TERMINAL)

    task_artifacts.cleanup(c.state_dir, now=datetime(2026, 10, 27, 9, 59, tzinfo=timezone.utc))
    response = _get(c, "review-answers", task)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert _Page(response.text).checked("track") == ["critical"]

    task_artifacts.cleanup(c.state_dir, now=datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc))
    [entry] = [i for i in _listing(c, task) if i["id"] == "review-answers"]
    assert entry["status"] == "expired"
    assert _get(c, "review-answers", task).status_code == 410
