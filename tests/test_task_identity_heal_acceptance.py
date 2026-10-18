"""Acceptance tests for ticket 02 of plan-gate-stage-loss: every launch and
resume heals the task identity file (.agent/task.json).

Spec: specs/plan-gate-stage-loss/spec.md, requirements 4-5 and their scenarios.
Black-box, through dispatcher.main.run_pass over the fakes of tests/test_main.py
with a real worktree, and dispatcher.waitd.handle_ping for the turn end.
"""
import json

import pytest

import dispatcher.main as main
from dispatcher.state import read_session
from dispatcher.waitd import handle_ping

from tests.test_main import FakeSessions, deps
from tests.test_plan_gate_stage_loss_acceptance import _approve, _gated, _setup
from tests.test_plan_review_gate_acceptance import BRANCH, GATE, ISSUE, _task

TARGET = "portfolio_eval"
HEALED = {"issue": ISSUE, "target": TARGET, "branch": BRANCH}
OLD_SHAPE = json.dumps({"issue": ISSUE})
PLANTED = json.dumps({"issue": ISSUE, "target": TARGET, "branch": "main"})


class RecordingSessions(FakeSessions):
    """Snapshots the identity file at the moment each session starts."""

    def __init__(self, wt, **kw):
        super().__init__(**kw)
        self.wt = wt
        self.identity_at_start = []
        self.resume_ids = []

    def _snap(self):
        f = self.wt / ".agent" / "task.json"
        self.identity_at_start.append(f.read_text() if f.exists() else None)

    def spawn_stage(self, *a, **kw):
        self._snap()
        return super().spawn_stage(*a, **kw)

    def resume(self, *a, session_id=None, **kw):
        self._snap()
        self.resume_ids.append(session_id)
        return super().resume(*a, session_id=session_id, **kw)


def _identity(wt):
    return wt / ".agent" / "task.json"


def _parsed(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _wake_with(c, tmp_path, content, *, session):
    """A gated task at the gate, the identity file holding `content` (None: missing)."""
    wt = _gated(c, tmp_path, session=session)
    if content is None:
        _identity(wt).unlink(missing_ok=True)
    else:
        _identity(wt).write_text(content)
    _approve(c)
    sess = RecordingSessions(wt)
    main.run_pass(c, deps(sess=sess))
    return wt, sess


@pytest.mark.parametrize("session", [False, True], ids=["launch", "resume"])
@pytest.mark.parametrize("stale", [OLD_SHAPE, PLANTED], ids=["old-shape", "planted-branch"])
def test_a_launch_or_resume_writes_the_task_identity_before_the_session_starts(
        tmp_path, monkeypatch, session, stale):
    c = _setup(tmp_path, monkeypatch)

    wt, sess = _wake_with(c, tmp_path, stale, session=session)

    assert len(sess.spawned) + len(sess.resumed) == 1
    assert [_parsed(s) for s in sess.identity_at_start] == [HEALED]
    assert _parsed(_identity(wt).read_text()) == HEALED


@pytest.mark.parametrize("content", [None, "[]", "this is not json"],
                         ids=["missing", "array", "not-json"])
def test_a_missing_or_malformed_identity_file_is_healed_on_launch(
        tmp_path, monkeypatch, content):
    c = _setup(tmp_path, monkeypatch)

    wt, sess = _wake_with(c, tmp_path, content, session=False)

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    assert [_parsed(s) for s in sess.identity_at_start] == [HEALED]
    assert _parsed(_identity(wt).read_text()) == HEALED


def _stop_hook_ping(wt, session_id):
    """What hooks/stop-hook.sh sends: the identity from task.json plus the session id."""
    task = json.loads(_identity(wt).read_text())
    return json.dumps({"issue": task["issue"], "target": task.get("target", ""),
                       "session_id": session_id}).encode()


def test_a_healed_worktree_records_and_resumes_its_conversation(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _wake_with(c, tmp_path, OLD_SHAPE, session=False)
    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]

    # the session ends its turn: the Stop hook pings, the task parks
    sess.alive_set.add(ISSUE)
    handle_ping(_stop_hook_ping(wt, "abc-123"), c.state_dir)
    main.run_pass(c, deps(sess=sess))
    rec = read_session(c.state_dir, TARGET, ISSUE)
    assert rec is not None and rec.session_id == "abc-123"

    # the operator wakes it: the recorded conversation continues
    _approve(c)
    sess.alive_set.discard(ISSUE)
    main.run_pass(c, deps(sess=sess))

    assert len(sess.spawned) == 1
    assert sess.resume_ids == ["abc-123"]
    assert _task(c).stage.value == GATE
