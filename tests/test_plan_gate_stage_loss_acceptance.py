"""Acceptance tests for ticket 01 of plan-gate-stage-loss: a plan launch of a
gated task runs at the gate stage.

Spec: specs/plan-gate-stage-loss/spec.md, requirements 1-3 and their scenarios.
Black-box, through dispatcher.main.run_pass over the fakes of tests/test_main.py,
with a real worktree behind each task.
"""
import json
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import eventlog, intents as intents_mod
from dispatcher.machine import PLAN_NO_APPROVAL
from dispatcher.models import parse_policy
from dispatcher.state import NO_SLOT, PARK_HUMAN, PARK_REVIEW, PlanApprovalRequest, Stage, load

from tests.test_main import (FakeNotifier, FakeSessions, deps, make_task,
                             patch_usage, patch_workspace)
from tests.test_plan_review_gate_acceptance import (
    GATE, ISSUE, SPEC, SUMMARY, _arm, _done, _folder_files, _plan_task, _ready,
    _signal, _task)
from tests.test_plan_review_gate_acceptance import _cfg as _gate_cfg

NEW_CONVERSATION = "new conversation: no session recorded"


def _policy():
    def track(name):
        m = ["claude-sonnet-5"]
        return {"when": f"The {name} track.", "spec": m, "plan": m,
                "implement": m, "review": m}
    return parse_policy({
        "triage": ["claude-sonnet-5@low"], "untracked": "standard",
        "tracks": {n: track(n) for n in ("standard", "research")}})


def _setup(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    return dc_replace(_gate_cfg(tmp_path), models=_policy())


def _gated(c, tmp_path, *, tickets=1, session=False, **kw):
    """A gated task, parked at the gate for review; `session` records a conversation."""
    return _plan_task(c, tmp_path, Stage(GATE), git=False, tickets=tickets,
                      session=session, gated=True, park=PARK_REVIEW,
                      operator_request=PlanApprovalRequest(SUMMARY), **kw)


def _approve(c):
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", ISSUE,
                             {"text": "approve"}, actor="op", epoch_ms=1)


def _resumed(c):
    return [e for e in eventlog.read_tail(c.state_dir)
            if e["event"] == "resumed" and e["issue"] == ISSUE]


def _implement_spawns(sess):
    return [s for s in sess.spawned if s[1] == "implement"]


def _fresh_launch_from_the_gate(c, tmp_path):
    wt = _gated(c, tmp_path)
    _ready(wt)
    _approve(c)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    sess.alive_set.add(ISSUE)
    return wt, sess


def test_wake_at_the_gate_with_no_session_record_keeps_the_gate_stage(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gated(c, tmp_path)
    _approve(c)
    sess = FakeSessions()

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    assert [e["detail"] for e in _resumed(c)] == [NEW_CONVERSATION]
    assert _task(c).stage.value == GATE


def test_approval_after_a_fresh_launch_starts_implement(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _fresh_launch_from_the_gate(c, tmp_path)
    _done(wt)
    d = deps(sess=sess)

    main.run_pass(c, d)

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT and t.track == "standard"
    assert t.ticket_count == 1
    assert len(_implement_spawns(sess)) == 1
    assert "implement_started" in d.notifier.sent
    assert "awaiting_plan_review" not in d.notifier.sent


def test_approval_with_a_new_track_after_a_fresh_launch(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt, sess = _fresh_launch_from_the_gate(c, tmp_path)
    _done(wt, track="research")

    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT and t.track == "research"
    assert len(_implement_spawns(sess)) == 1


def test_resumed_conversation_at_the_gate_is_unchanged(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _gated(c, tmp_path, session=True)
    _approve(c)
    sess = FakeSessions()

    main.run_pass(c, deps(sess=sess))

    assert sess.spawned == [] and len(sess.resumed) == 1
    assert [e["detail"] for e in _resumed(c)] == [""]
    assert _task(c).stage.value == GATE


@pytest.mark.parametrize("used", [0, 1])
def test_respawn_of_a_dead_session_at_the_gate_keeps_the_gate_stage(
        tmp_path, monkeypatch, used):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, Stage(GATE), git=False, gated=True,
                    unattended_rounds=used, operator_request=PlanApprovalRequest(SUMMARY))
    _ready(wt)
    _arm(c)
    sess = FakeSessions(alive=set())

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    t = _task(c)
    assert t.stage.value == GATE and t.unattended_rounds == used + 1

    sess.alive_set.add(ISSUE)
    _ready(wt)   # the new session reports its plan ready again
    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.unattended_rounds == used + 1   # the respawn and this report are one round
    assert t.stage.value == GATE
    assert t.operator_request is not None and t.operator_request.path == SUMMARY
    if used == 0:
        assert t.park == ""   # below the cap the first ready report starts a fresh grace period
    assert sess.resumed == []   # a ready report is not bounced back to the session


def test_crash_resume_of_a_gated_task_runs_at_the_gate_stage(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, Stage.FAILED, git=False, tickets=1, gated=True,
                    slot=NO_SLOT, crashed_stage="plan", unattended_rounds=1)
    (wt / ".agent" / "review-answers.json").write_text(
        json.dumps({"answers": [{"question": "approve the plan?", "answer": "approve"}]}))
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", ISSUE, {}, "op", 1)
    sess = FakeSessions()

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    t = _task(c)
    assert t.stage.value == GATE and t.unattended_rounds == 0

    sess.alive_set.add(ISSUE)
    _done(wt)
    main.run_pass(c, deps(sess=sess))

    t = _task(c)
    assert t.stage is Stage.IMPLEMENT and t.ticket_count == 1
    assert len(_implement_spawns(sess)) == 1


def test_crash_resume_of_a_plan_that_never_reached_the_gate_runs_in_stage_plan(
        tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    _plan_task(c, tmp_path, Stage.FAILED, git=False, gated=False,
               slot=NO_SLOT, crashed_stage="plan")
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", ISSUE, {}, "op", 1)
    sess = FakeSessions()

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    assert _task(c).stage is Stage.PLAN


def test_plan_launch_from_spec_still_sets_stage_plan(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = make_task(c, issue=ISSUE, stage=Stage.SPEC, track="standard")
    _folder_files(wt, design=False)
    _signal(wt, stage="spec", status="done", artifact=SPEC, track="standard",
            note="stage 1 written")
    sess = FakeSessions(alive={ISSUE})

    main.run_pass(c, deps(sess=sess))

    assert [s[:2] for s in sess.spawned] == [(ISSUE, "plan")]
    assert _task(c).stage is Stage.PLAN


def test_done_from_a_gated_task_in_stage_plan_parks_with_a_clear_note(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, Stage.PLAN, git=False, tickets=1, gated=True)
    _done(wt)
    sess = FakeSessions(alive={ISSUE})
    d = deps(sess=sess)

    main.run_pass(c, d)

    assert sess.resumed == [] and _implement_spawns(sess) == []
    assert "plan_retry" not in d.notifier.sent
    t = _task(c)
    assert t.park == PARK_HUMAN and t.plan_retries == 0
    note = " ".join(t.park_note.lower().split())
    assert "gate state was lost" in note
    assert "standard" in note
    assert "by hand" in note and "implement" in note


def test_done_from_an_ungated_task_in_stage_plan_still_bounces(tmp_path, monkeypatch):
    c = _setup(tmp_path, monkeypatch)
    wt = _plan_task(c, tmp_path, Stage.PLAN, git=False, tickets=1, gated=False,
                    plan_retries=0)
    _done(wt)
    sess = FakeSessions(alive={ISSUE})

    main.run_pass(c, deps(sess=sess))

    (resume,) = sess.resumed
    assert PLAN_NO_APPROVAL in resume[1]
