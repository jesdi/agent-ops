"""Acceptance tests for ticket 01 of the review page: the plan gate accepts a
review page by shape.

Spec: specs/2026-10-07-review-page/spec.md, requirements 1, 3 and 15 and the
scenarios "the plan session signals the gate with the page", "a page without
the template marker is bounced" and "a page over the size limit is bounced".
Black-box, through dispatcher.main.run_pass over the fakes of tests/test_main.py
(helpers of tests/test_plan_review_gate_acceptance.py), the console route
GET /api/task/{target}/{issue}/request and the rendered plan prompt.
"""
import os
import re
from pathlib import Path

import pytest

import dispatcher
import dispatcher.artifacts as artifacts
import dispatcher.main as main
from dispatcher.state import Stage

from tests.test_main import FakeSessions, deps
from tests.test_plan_review_gate_acceptance import (
    GATE, ISSUE, _plan_prompt, _plan_task, _setup, _signal, _task)

PAGE = ".agent/review.html"
MARKER = '<meta name="agent-ops-review" content="1">'
FIXTURE = Path(__file__).parent / "fixtures" / "review-page.html"
KIB = 1024


def _padded(size, text=None):
    """`text` (the fixture) with an HTML comment so the file is `size` bytes."""
    text = FIXTURE.read_text() if text is None else text
    pad = size - len(text.encode()) - len("<!--  -->")
    assert pad >= 0
    return text + "<!-- " + "x" * pad + " -->"


def _ready(wt, artifact=PAGE, **kw):
    _signal(wt, **{"stage": "plan", "status": "awaiting-review",
                   "note": "plan ready", "artifact": artifact,
                   "open_questions": 3, **kw})


def _pass(c, wt, page=None, **kw):
    """Run one pass over a plan task whose session signals the gate."""
    if page is not None:
        (wt / PAGE).write_text(page)
    _ready(wt, **kw)
    sess = FakeSessions(alive={ISSUE})
    main.run_pass(c, deps(sess=sess))
    return sess


def _bounced(c, sess):
    t = _task(c)
    assert t.stage is Stage.PLAN
    assert t.operator_request is None
    assert t.plan_retries == 1
    (resume,) = sess.resumed
    return resume[1]


def test_page_signal_enters_the_gate_and_the_console_serves_the_html(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS
    from web.app import create_app
    from web.sources import Sources
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)
    page = _padded(20 * KIB)

    _pass(c, wt, page)

    t = _task(c)
    assert t.stage.value == GATE
    assert t.operator_request.kind == "plan-approval"
    assert t.operator_request.path == PAGE
    with TestClient(create_app(c, Sources(c, sessions=None, github=None))) as client:
        r = client.get(f"/api/task/portfolio_eval/{ISSUE}/request", headers=HEADERS)
    assert r.status_code == 200, r.text
    content = r.json()["content"]
    assert content["path"] == PAGE
    assert content["media_type"] == "text/html"
    assert content["text"] == page


def test_page_without_the_marker_is_bounced(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)

    sess = _pass(c, wt, _padded(12 * KIB, "<!doctype html><title>x</title>"))

    assert "review page lacks the template marker" in _bounced(c, sess)


def test_page_over_256_kib_is_bounced(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)

    sess = _pass(c, wt, _padded(300 * KIB))

    assert "exceeds 256 KiB" in _bounced(c, sess)


def test_symlink_as_the_page_does_not_enter_the_gate(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)
    (wt / PAGE).symlink_to(FIXTURE)

    sess = _pass(c, wt)

    _bounced(c, sess)


@pytest.mark.parametrize("where", ["absolute", "dotdot"])
def test_path_outside_the_worktree_does_not_enter_the_gate(tmp_path, monkeypatch, where):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)
    outside = tmp_path / "outside.html"
    outside.write_text(_padded(20 * KIB))
    artifact = str(outside) if where == "absolute" else os.path.relpath(outside, wt)

    sess = _pass(c, wt, artifact=artifact)

    _bounced(c, sess)


def test_the_markdown_summary_is_no_longer_a_gate_artifact(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, git=False)   # writes a valid .agent/plan-review.md

    sess = _pass(c, wt, artifact=".agent/plan-review.md", open_questions=0)

    _bounced(c, sess)


def test_no_code_reads_the_summary_format():
    root = Path(dispatcher.__file__).parent
    hits = [p.name for p in root.rglob("*.py")
            and "open questions" in p.read_text(errors="replace").lower()]
    assert hits == []
    assert not hasattr(artifacts, "PLAN_SUMMARY")


def test_plan_prompt_asks_for_the_page_from_the_skill(tmp_path, monkeypatch):
    p = _plan_prompt(tmp_path, monkeypatch)

    assert ".agent/review.html" in p
    assert "review-page" in p and "plan mode" in p
    assert re.search(r"blocked[\s\S]{0,300}review-page skill not installed", p)
    assert "plan-review.md" not in p


def test_fixture_is_the_template_with_every_prototype_section():
    text = FIXTURE.read_text()

    assert MARKER in text
    for part in ('class="tickets"', 'data-q=', 'class="q"', 'class="corr"',
                 'class="tracks"', 'class="bar"'):
        assert part in text, part
    assert "http://" not in text and "https://" not in text
