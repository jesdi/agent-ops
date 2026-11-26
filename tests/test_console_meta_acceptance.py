"""Acceptance tests for ticket 06 of the review send delivery: the dispatcher
stamps the console link into the review page when it arms the request.

Spec: specs/2026-11-24-review-send-delivery/spec.md, requirement 2 and
scenarios 3, 4 and 5. Black-box: a dispatcher pass (dispatcher.main.run_pass
over the fakes of tests/dispatchfakes.py) on a task whose session reports
`awaiting-answers` or `awaiting-review`, then the page on disk and
GET /api/task/{repository}/{issue}/request. The negative cases each carry a
control task in the same pass whose page is stamped.
"""
import json
import re
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher.artifacts import page_revision
from dispatcher.state import (SessionRecord, Stage, TaskState, load, save,
                              write_session, operator_request)
from dispatcher.status import Kind

from tests.dispatchfakes import (FakeSessions, armed_request, cfg, deps, make_task,
                                 patch_usage, patch_workspace)
from tests.statuses import RUNNING
from tests.test_plan_review_gate_acceptance import (
    ISSUE as PLAN_ISSUE, _plan_task, _ready, _setup as _plan_setup, _task as _plan_task_state)

REPOSITORY = "portfolio_eval"
ISSUE = 42
OTHER = 43
CONSOLE = "https://ops.example"
PAGE = ".agent/questionnaire.html"
FIXTURE = Path(__file__).parent / "fixtures" / "review-page.html"
META = re.compile(r'<meta\s+name="agent-ops-console"\s+content="([^"]*)"\s*/?>')


def _meta(repository=REPOSITORY, issue=ISSUE, console=CONSOLE):
    return (f'<meta name="agent-ops-console" '
            f'content="{console}/task/{repository}/{issue}/review">')


def _setup(tmp_path, monkeypatch, console_url=CONSOLE):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return dc_replace(cfg(tmp_path), console_url=console_url)


def _report(wt, artifact=PAGE):
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "q",
         "artifact": artifact}))


def _spec_task(c, issue=ISSUE, page=None, slot=0):
    """A spec session that reports its questionnaire; `page` is written when given."""
    wt = make_task(c, issue=issue, stage=Stage.SPEC, slot=slot)
    if page is not None:
        (wt / PAGE).write_text(page)
    _report(wt)
    return wt


def _pass(c, *alive):
    main.run_pass(c, deps(sess=FakeSessions(alive=set(alive or {ISSUE}))))


def _task(c, issue=ISSUE, repository=REPOSITORY):
    return load(c.state_dir, repository, issue)


def _request(c, repository=REPOSITORY, issue=ISSUE):
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS
    from web.app import create_app
    from web.sources import Sources
    with TestClient(create_app(c, Sources(c.state_dir, sessions=None, github=None))) as client:
        r = client.get(f"/api/task/{repository}/{issue}/request", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _metas(path):
    return META.findall(Path(path).read_text())


def _rearm(c, issue=ISSUE):
    """The session ran again and reports the same page."""
    t = _task(c, issue)
    save(c.state_dir, dc_replace(t, status=RUNNING, slot=0))
    _report(Path(t.worktree))


# Scenario 3: the meta is written before the revision.
def test_parked_questionnaire_holds_one_console_meta_and_the_request_names_its_revision(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _spec_task(c, page=FIXTURE.read_text())

    _pass(c)

    assert _task(c).status.kind is Kind.PARKED_ANSWERS
    assert _metas(wt / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{ISSUE}/review"]
    on_disk, problem = page_revision(wt / PAGE)
    assert problem == ""
    assert operator_request(_task(c)).fingerprint == on_disk
    assert _request(c)["revision"] == on_disk


def test_plan_page_held_at_the_gate_is_stamped_and_the_plan_revision_covers_it(
        tmp_path, monkeypatch):
    c = _plan_setup(tmp_path, monkeypatch, console_url=CONSOLE)
    wt = _plan_task(c, tmp_path, git=False)
    (wt / ".agent" / "review.html").write_text(FIXTURE.read_text())
    _ready(wt)

    main.run_pass(c, deps(sess=FakeSessions(alive={PLAN_ISSUE})))

    t = _plan_task_state(c)
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL
    assert _metas(wt / ".agent" / "review.html") == [
        f"{CONSOLE}/task/{REPOSITORY}/{PLAN_ISSUE}/review"]
    stamped = armed_request(t, ".agent/review.html").fingerprint
    assert operator_request(t).fingerprint == stamped
    assert _request(c, issue=PLAN_ISSUE)["revision"] == stamped


# Scenario 4: re-arming never duplicates the meta.
def test_a_meta_with_an_old_console_url_is_replaced_by_one_with_the_new_url(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    old = _meta(console="https://old.example")
    wt = _spec_task(c, page=FIXTURE.read_text().replace(
        '<meta name="agent-ops-review" content="1">',
        '<meta name="agent-ops-review" content="1">' + old, 1))
    assert _metas(wt / PAGE) == ["https://old.example/task/portfolio_eval/42/review"]

    _pass(c)

    assert _metas(wt / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{ISSUE}/review"]
    assert _request(c)["revision"] == page_revision(wt / PAGE)[0]


def test_reading_the_same_report_again_keeps_the_page_bytes_and_the_revision(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _spec_task(c, page=FIXTURE.read_text())
    _pass(c)
    first = (wt / PAGE).read_bytes()
    revision = _request(c)["revision"]
    assert len(_metas(wt / PAGE)) == 1

    _rearm(c)
    _pass(c)

    assert (wt / PAGE).read_bytes() == first
    assert _request(c)["revision"] == revision
    assert len(_metas(wt / PAGE)) == 1


# Scenario 5: no console URL, or an unwritable page.
def test_without_a_console_url_the_page_is_unchanged_and_the_request_is_armed(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch, console_url="")
    wt = _spec_task(c, page=FIXTURE.read_text())
    before = (wt / PAGE).read_bytes()

    _pass(c)

    assert (wt / PAGE).read_bytes() == before
    assert _task(c).status.kind is Kind.PARKED_ANSWERS
    assert _request(c)["revision"] == page_revision(wt / PAGE)[0]

    # Control: once the URL is configured, the next arming stamps the page.
    c = dc_replace(c, console_url=CONSOLE)
    _rearm(c)
    _pass(c)
    assert _metas(wt / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{ISSUE}/review"]


def test_a_symlinked_page_is_not_written_through(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    target = tmp_path / "target.html"
    target.write_text(FIXTURE.read_text())
    before = target.read_bytes()
    wt = _spec_task(c)
    (wt / PAGE).symlink_to(target)
    control = _spec_task(c, issue=OTHER, page=FIXTURE.read_text(), slot=1)

    _pass(c, ISSUE, OTHER)

    assert target.read_bytes() == before
    assert (wt / PAGE).is_symlink()
    assert _metas(control / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{OTHER}/review"]


def test_a_page_outside_the_worktree_is_not_written_to(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    outside = tmp_path / "outside.html"
    outside.write_text(FIXTURE.read_text())
    before = outside.read_bytes()
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC)
    _report(wt, artifact=str(outside))
    control = _spec_task(c, issue=OTHER, page=FIXTURE.read_text(), slot=1)

    _pass(c, ISSUE, OTHER)

    assert outside.read_bytes() == before
    assert _metas(control / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{OTHER}/review"]


def test_a_page_without_the_template_marker_gets_no_meta_and_is_armed_as_today(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _spec_task(c, page="<!doctype html><title>x</title><p>no marker</p>")
    before = (wt / PAGE).read_bytes()
    control = _spec_task(c, issue=OTHER, page=FIXTURE.read_text(), slot=1)

    _pass(c, ISSUE, OTHER)

    assert (wt / PAGE).read_bytes() == before
    req = operator_request(_task(c))
    assert req.path == PAGE and req.fingerprint == ""
    assert _metas(control / PAGE) == [f"{CONSOLE}/task/{REPOSITORY}/{OTHER}/review"]


# The meta's content: URL-encoded path under the HTML-escaped base.
def test_the_meta_encodes_the_repository_and_escapes_the_console_url(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    base = cfg(tmp_path)
    repository = dc_replace(base.repositories[0], name="my widget")
    c = dc_replace(base, console_url="https://ops.example/a&b", repositories=[repository])
    wt = Path(repository.worktrees_path) / f"task-{ISSUE}"
    (wt / ".agent").mkdir(parents=True)
    save(c.state_dir, TaskState(issue=ISSUE, repository="my widget", stage=Stage.SPEC,
                                slot=0, worktree=str(wt), branch=f"agent/task-{ISSUE}",
                                title="t", updated_at="2026-07-21T00:00:00+00:00",
                                track="standard", status=RUNNING))
    write_session(c.state_dir, "my widget", ISSUE, SessionRecord("recorded-session", "spec"))
    (wt / PAGE).write_text(FIXTURE.read_text())
    _report(wt)

    _pass(c)

    text = (wt / PAGE).read_text()
    assert ('<meta name="agent-ops-console" '
            'content="https://ops.example/a&amp;b/task/my%20widget/42/review">') in text
    assert len(_metas(wt / PAGE)) == 1
    on_disk, problem = page_revision(wt / PAGE)
    assert problem == ""
    assert _request(c, repository="my%20widget")["revision"] == on_disk
