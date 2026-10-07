"""Ticket 06: the task view carries the implement progress the session
reported in .agent/stage.json. Real Sources over a real state dir and a real
worktree directory; only the session and GitHub clients are stubbed."""
import json

import pytest
from fastapi.testclient import TestClient

from dispatcher import state
from dispatcher.state import Stage
from tests.webfakes import HEADERS, make_config, make_task
from web.app import create_app
from web.sources import Sources


class _NoSessions:
    def is_alive(self, target, issue):
        return False

    def capture_tail(self, target, issue):
        return ""


class _NoGithub:
    def issue_state(self, repo, number):
        return "OPEN"


def _progress(tmp_path, stage, signal):
    """signal: dict -> JSON file, str -> raw file content, None -> no file."""
    wt = tmp_path / "wt"
    (wt / ".agent").mkdir(parents=True)
    if signal is not None:
        raw = signal if isinstance(signal, str) else json.dumps(signal)
        (wt / ".agent" / "stage.json").write_text(raw)
    sdir = tmp_path / "state"
    sdir.mkdir()
    cfg = make_config(sdir)
    state.save(sdir, make_task(issue=7, stage=stage, worktree=str(wt)))
    client = TestClient(create_app(
        cfg, Sources(cfg, _NoSessions(), _NoGithub())))
    r = client.get("/api/task/alpha/7", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert "implement_progress" in body
    return body["implement_progress"]


def _sig(note, stage="implement", status="working"):
    return {"stage": stage, "status": status, "note": note}


def test_implement_progress_is_the_reported_note(tmp_path):
    got = _progress(tmp_path, Stage.IMPLEMENT, _sig("2/4 tickets merged"))
    assert got == "2/4 tickets merged"


@pytest.mark.parametrize("signal", [
    None, _sig(""), _sig("   "), {"stage": "implement", "status": "working"},
])
def test_no_progress_yet_is_null(tmp_path, signal):
    assert _progress(tmp_path, Stage.IMPLEMENT, signal) is None


@pytest.mark.parametrize("stage", [
    Stage.PLAN, Stage.AWAITING_PLAN_REVIEW, Stage.REVIEW, Stage.PR_OPEN,
    Stage.FAILED, Stage.DONE])
def test_old_report_is_ignored_outside_implement(tmp_path, stage):
    got = _progress(tmp_path, stage, _sig("3/4 tickets merged"))
    assert got is None


@pytest.mark.parametrize("signal", [
    _sig("3/4 tickets merged", stage="plan"),
    "{not json", "[]", "null",
    _sig(3), _sig(["3/4"]),
])
def test_unusable_report_is_null_and_endpoint_answers(tmp_path, signal):
    assert _progress(tmp_path, Stage.IMPLEMENT, signal) is None


@pytest.mark.parametrize("field", ["run_id", "round"])
def test_number_too_large_for_an_integer_is_null_and_endpoint_answers(tmp_path, field):
    raw = ('{"stage": "implement", "status": "working", "note": "2/4 tickets merged", '
           f'"{field}": 1e400}}')
    assert _progress(tmp_path, Stage.IMPLEMENT, raw) is None
