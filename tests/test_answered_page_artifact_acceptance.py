"""Acceptance tests for #282: an answers artifact opens as the page it
answered, with the submitted answers, and survives the worktree and the next
round.

Black-box: the operator's submission is an `answers` intent drained by one
dispatcher pass; the artifact is opened through the console API, as the
Artifacts panel link does.
"""
import hashlib
import json
import shutil

from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher import answers, intents, operator_intents, task_artifacts
from dispatcher.state import Stage, load
from dispatcher.status import Kind
from tests.dispatchfakes import FakeSessions, cfg, deps, make_task, patch_usage, patch_workspace
from tests.webfakes import HEADERS, FakeSources, make_config, make_repository
from web.app import create_app

ISSUE = 412
REPOSITORY = "portfolio_eval"
PAGE = b"""<!doctype html><html><head><meta name="agent-ops-review" content="1"></head><body>
<div class="q" data-q="format"><h2>Which export format?</h2>
<label><input type="radio" name="format" value="csv"> CSV</label>
<label><input type="radio" name="format" value="parquet"> Parquet</label>
<textarea id="note-format"></textarea></div>
</body></html>"""
ANSWERS = {"format": "parquet", "format.note": "keep it columnar"}
URL = f"/api/task/{REPOSITORY}/{ISSUE}/artifacts/questionnaire-answers"


def _answered(tmp_path, monkeypatch):
    """Issue 412 parked on its questionnaire, then answered with "Send answers"."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC)
    (wt / ".agent" / "questionnaire.html").write_bytes(PAGE)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "q",
         "artifact": ".agent/questionnaire.html"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={ISSUE})))
    assert load(c.state_dir, REPOSITORY, ISSUE).status.kind is Kind.PARKED_ANSWERS
    intents.write_intent(c.state_dir, "answers", REPOSITORY, ISSUE,
                         {"answers": ANSWERS, "submit": "changes",
                          "revision": hashlib.sha256(PAGE).hexdigest()},
                         actor="jesdi", epoch_ms=1)
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={ISSUE})))
    assert (wt / ".agent" / "questionnaire-answers.json").exists()
    return c, wt


def _open(c):
    sources = FakeSources(c.state_dir)
    sources.tasks_list = [load(c.state_dir, REPOSITORY, ISSUE)]
    config = make_config(c.state_dir, [make_repository(REPOSITORY, "jesdi/portfolio_eval")])
    return TestClient(create_app(config, sources)).get(URL, headers=HEADERS)


def _assert_answered_page(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert not body.lstrip().startswith("{"), "the answers open as raw JSON"
    assert "Which export format?" in body       # the page it answered
    assert "keep it columnar" in body           # with the submitted answers


def test_the_answers_artifact_opens_as_the_answered_page(tmp_path, monkeypatch):
    c, _ = _answered(tmp_path, monkeypatch)
    task_artifacts.collect(c.state_dir, load(c.state_dir, REPOSITORY, ISSUE), "", publish=False)

    _assert_answered_page(_open(c))


def test_the_answered_page_survives_worktree_loss(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    shutil.rmtree(wt)
    task_artifacts.collect(c.state_dir, load(c.state_dir, REPOSITORY, ISSUE), "", publish=False)

    _assert_answered_page(_open(c))


def test_the_answered_page_survives_the_next_round(tmp_path, monkeypatch):
    c, wt = _answered(tmp_path, monkeypatch)
    (wt / ".agent" / "questionnaire.html").write_bytes(PAGE.replace(b"export", b"import"))
    answers.discard_stale(wt, "answers", "rev-2")
    assert not (wt / ".agent" / "questionnaire-answers.json").exists()
    task_artifacts.collect(c.state_dir, load(c.state_dir, REPOSITORY, ISSUE), "", publish=False)

    _assert_answered_page(_open(c))
