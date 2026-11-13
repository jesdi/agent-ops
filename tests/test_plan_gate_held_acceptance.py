"""Acceptance tests for ticket 05 of #132: the plan gate is held, then parked.

Spec: specs/2026-11-10-task-lifecycle-state-machine/spec.md, requirement 8
and its scenarios. Black-box through dispatcher.main.run_pass over the fakes
of tests/test_main.py, observed through the task's status, events.jsonl and
the fakes."""
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.main as main
from dispatcher import eventlog, intents as intents_mod, spec_publish
from dispatcher.artifacts import plan_revision
from dispatcher.lifecycle import (GATE_STOPPED, PLAN_NO_APPROVAL, At, Kind, Status,
                                  operator_request)
from dispatcher.state import NO_SLOT, PlanApprovalRequest, Stage, at_gate, load, save

from tests.test_main import (PLAN_SUMMARY, SPEC_PATH, POLICY, FakeSessions, cfg, deps,
                             make_task, patch_usage, patch_workspace, write_tickets)

T, N = "portfolio_eval", 42
HELD, PARKED_REVIEW = Kind.HELD_PLAN_APPROVAL, Kind.PARKED_REVIEW


def _ago(minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def published(monkeypatch):
    """What the publish step was asked for."""
    asked = []

    def publish(**kw):
        asked.append(kw)
        return spec_publish.PublishResult(url="https://x/spec")

    monkeypatch.setattr(main.spec_publish, "ensure_published", publish)
    return asked


@pytest.fixture
def c(tmp_path, monkeypatch, published):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    tracks = {**POLICY.tracks,
              "trivial": dc_replace(POLICY.tracks["trivial"], plan_review=False)}
    return dc_replace(cfg(tmp_path), models=dc_replace(POLICY, tracks=tracks))


def _signal(wt, status, **extra):
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "plan", "status": status, **extra}))


def _ready(wt):
    _signal(wt, "awaiting-review", artifact=PLAN_SUMMARY, note="plan ready",
            open_questions=0)


def _plan(c, stage=Stage.PLAN, **kw):
    """A plan-stage task in slot 0 with two good tickets and a review page."""
    kw.setdefault("updated_at", _ago())
    wt = make_task(c, issue=N, stage=stage, spec_path=SPEC_PATH, **kw)
    write_tickets(wt, 2)
    return wt


def _fingerprint(wt):
    return plan_revision(wt, PLAN_SUMMARY, SPEC_PATH).fingerprint


def _held(c, minutes=0, **kw):
    """`stage=plan, held(plan-approval(page, fp))` armed for the plan on
    disk, its ready report re-read on every pass."""
    wt = _plan(c, Stage.AWAITING_PLAN_REVIEW, gated=True, updated_at=_ago(minutes), **kw)
    t = _task(c)
    save(c.state_dir, dc_replace(t, operator_request=PlanApprovalRequest(
        PLAN_SUMMARY, fingerprint=_fingerprint(wt))))
    _ready(wt)
    assert _task(c).at == At(Stage.PLAN, Status(HELD, path=PLAN_SUMMARY,
                                                fingerprint=_fingerprint(wt)))
    return wt


def _task(c):
    return load(c.state_dir, T, N)


def _run(c, sess=None, passes=1):
    d = deps(sess=sess if sess is not None else FakeSessions(alive={N}))
    for _ in range(passes):
        main.run_pass(c, d)
    return d


def _transitions(c):
    return [e["transition"] for e in eventlog.read_tail(c.state_dir)
            if e["event"] == "transition"]


def test_plan_gate_holds_during_grace(c, published):
    wt = _plan(c)
    _ready(wt)
    d = _run(c)
    t, fp = _task(c), _fingerprint(wt)
    assert t.at == At(Stage.PLAN, Status(HELD, path=PLAN_SUMMARY, fingerprint=fp))
    assert (t.slot, t.gated) == (0, True)
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY, fingerprint=fp)
    assert "awaiting_plan_review" in d.notifier.sent and len(published) == 1


def test_plan_gate_is_skipped(c):
    wt = _plan(c, track="trivial")
    _ready(wt)
    d = _run(c)
    t = _task(c)
    assert t.at.stage is Stage.IMPLEMENT and operator_request(t) is None
    assert (t.gated, t.asked) == (False, False)
    assert _transitions(c) == [{"from": "running", "event": "session-signal",
                                "to": "running", "stage": "implement"}]
    assert "awaiting_plan_review" not in d.notifier.sent


def test_plan_gate_grace_expires(c):
    wt = _held(c, minutes=16)
    d = _run(c)
    t, fp = _task(c), _fingerprint(wt)
    assert t.at == At(Stage.PLAN, Status(PARKED_REVIEW, path=PLAN_SUMMARY, fingerprint=fp,
                                         note="plan ready for review"))
    assert t.slot == NO_SLOT and N in d.sessions.ended
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY, fingerprint=fp)


def test_plan_gate_session_dies_during_grace(c):
    _held(c)
    before = _task(c)
    d = _run(c, FakeSessions(alive=set()))
    t = _task(c)
    assert [s[:2] for s in d.sessions.spawned] == [(N, "plan")]
    assert t.worktree == before.worktree and t.slot == 0
    assert t.at == At(Stage.PLAN, Status(Kind.RUNNING))


def test_plan_gate_session_dies_with_no_rounds_left(c):
    wt = _held(c, unattended_rounds=2)
    d = _run(c, FakeSessions(alive=set()))
    t, fp = _task(c), _fingerprint(wt)
    assert t.at == At(Stage.PLAN, Status(PARKED_REVIEW, path=PLAN_SUMMARY, fingerprint=fp,
                                         note=GATE_STOPPED))
    assert "plan_session_stopped" in d.notifier.sent and d.sessions.spawned == []


def test_a_rework_session_that_dies_with_no_rounds_left_parks_for_review_armed_for_no_plan(c):
    """A session reworking the plan (its request disarmed) offers no checked
    plan: it parks for review on the prescribed page, armed for no revision,
    so nothing can be approved before a ready report passes the check."""
    wt = _plan(c, Stage.AWAITING_PLAN_REVIEW, gated=True, unattended_rounds=2)
    _signal(wt, "working")
    d = _run(c, FakeSessions(alive=set()))
    t = _task(c)
    assert t.at == At(Stage.PLAN, Status(PARKED_REVIEW, path=PLAN_SUMMARY, note=GATE_STOPPED))
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY, fingerprint="")
    assert "plan_session_stopped" in d.notifier.sent and t.slot == NO_SLOT


def test_plan_approval_moves_to_implement(c):
    wt = _held(c)
    intents_mod.write_intent(c.state_dir, "answers", T, N, {
        "submit": "approve", "revision": _fingerprint(wt), "answers": {}},
        actor="op", epoch_ms=1)
    sess = FakeSessions(alive={N})
    _run(c, sess)                        # the approval wakes the gate session
    assert _task(c).at.status.kind in (Kind.RUNNING, Kind.WAKING)
    _signal(wt, "done", artifact=".agent/tickets", track="standard")
    _run(c, sess)
    t = _task(c)
    assert t.at.stage is Stage.IMPLEMENT and operator_request(t) is None
    assert [s[1] for s in sess.spawned] == ["implement"]


def test_done_with_no_approval_is_bounced(c):
    wt = _plan(c)
    _signal(wt, "done", track="standard")
    d = _run(c)
    assert _task(c).at == At(Stage.PLAN, Status(Kind.RUNNING))
    (resume,) = d.sessions.resumed
    assert PLAN_NO_APPROVAL in resume[1]


def test_a_gated_plan_task_that_signals_done_is_bounced_like_any_unapproved_done(c):
    wt = _plan(c, gated=True)
    _signal(wt, "done", track="standard")
    d = _run(c)
    t = _task(c)
    assert t.at == At(Stage.PLAN, Status(Kind.RUNNING)) and t.plan_slips == 1
    assert not at_gate(t)    # `gated` moved it nowhere: a second `done` is bounced too
    (resume,) = d.sessions.resumed
    assert PLAN_NO_APPROVAL in resume[1]
    assert d.sessions.spawned == []


def test_an_armed_ready_report_read_ten_times_keeps_the_grace_clock(c, monkeypatch):
    _held(c, minutes=5)
    before = _task(c)
    d = _run(c, passes=10)
    assert _task(c) == before and d.notifier.sent == []
    # Ten minutes on, the grace time (15) is over: it parks on schedule.
    later = datetime.now(timezone.utc) + timedelta(minutes=10, seconds=1)
    monkeypatch.setattr(main, "datetime", _Clock(later))
    _run(c)
    assert _task(c).at.status.kind is PARKED_REVIEW


class _Clock:
    """main.datetime with `now` moved to `at`."""

    def __init__(self, at):
        self.at = at

    def now(self, tz=None):
        return self.at

    def __getattr__(self, name):
        return getattr(datetime, name)


def test_a_ready_report_armed_for_another_revision_is_a_new_round_not_a_grace_park(c):
    wt = _held(c, minutes=16)
    (wt / PLAN_SUMMARY).write_text((wt / PLAN_SUMMARY).read_text() + "<p>changed</p>")
    d = _run(c)
    t, fp = _task(c), _fingerprint(wt)
    assert t.at == At(Stage.PLAN, Status(HELD, path=PLAN_SUMMARY, fingerprint=fp))
    assert (t.unattended_rounds, d.notifier.sent) == (1, ["awaiting_plan_review"])
    assert t.updated_at > _ago(1)        # the new round has a fresh grace clock
    assert N not in d.sessions.ended


def test_an_approval_with_a_failing_ticket_check_and_no_retry_left_fails(c):
    wt = _plan(c, Stage.AWAITING_PLAN_REVIEW, gated=True, plan_retries=1)
    (wt / ".agent" / "tickets" / "01-t1.md").write_text("# tiny\n")
    _signal(wt, "done", artifact=".agent/tickets", track="standard")
    d = _run(c)
    t = _task(c)
    # The legacy file keeps no reason or stage for an artifact failure (ticket 06).
    assert (t.stage, t.at.status.kind) == (Stage.FAILED, Kind.FAILED)
    assert "artifact_failed" in d.notifier.sent and d.sessions.spawned == []
