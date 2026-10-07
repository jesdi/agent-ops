"""Acceptance tests for ticket 05: null grace never parks.

Spec: specs/multi-project-box/spec.md, requirement 8 and scenarios
"null grace never parks" / "zero grace still parks immediately".
Black-box, through dispatcher.main.run_pass — mirrors the harness in
tests/test_main.py (gate_signal, cfg, make_task, deps, FakeSessions,
FakeGitHub, replace_capacity).
"""
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import dispatcher.main as main
from dispatcher.github import Candidate
from dispatcher.state import PARK_REVIEW, PlanApprovalRequest, load

from tests.test_main import (FakeGitHub, FakeSessions, arm_gate, cfg, deps,
                             gate_signal, make_task, patch_usage,
                             patch_workspace, replace_capacity,
                             write_tickets)

# A task that waits at the gate has its request armed.
ARMED = PlanApprovalRequest(".agent/review.html")


def test_null_grace_never_parks_after_12_hours(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(replace_capacity(cfg(tmp_path), 1),
                   spec_review_grace_minutes=None)
    twelve_hours_ago = (datetime.now(timezone.utc)
                        - timedelta(hours=12)).isoformat()
    wt = make_task(c, issue=42, stage=main.Stage.AWAITING_PLAN_REVIEW,
                   operator_request=ARMED,
                   updated_at=twelve_hours_ago)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])  # Ready candidate elsewhere
    sess = FakeSessions(alive={42})
    d = deps(gh, sess)

    main.run_pass(c, d)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.park == ""                       # not parked
    assert t.stage is main.Stage.AWAITING_PLAN_REVIEW
    assert sess.ended == []                    # session not ended
    assert "plan_parked" not in d.notifier.sent
    # Capacity: box has room for 1, and it's still held by the gate task, so
    # the Ready candidate elsewhere must NOT be claimed. If the unit had been
    # released (i.e. it were wrongly parked), this candidate would claim.
    assert gh.claimed == []


def test_zero_grace_parks_the_pass_after_it_entered_review(tmp_path, monkeypatch):
    """Mirrors test_zero_grace_parks_on_the_next_pass in test_main.py — kept
    here as a regression guard for the ticket's own checkbox wording."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, issue=42, stage=main.Stage.AWAITING_PLAN_REVIEW,
                   operator_request=ARMED,
                   updated_at=datetime.now(timezone.utc).isoformat())
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)

    main.run_pass(c, d)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.park == PARK_REVIEW
    assert sess.ended == [42]
    assert "plan_parked" in d.notifier.sent


def test_default_grace_does_not_park_before_15_minutes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)  # default spec_review_grace_minutes == 15
    just_under = (datetime.now(timezone.utc)
                 - timedelta(minutes=14, seconds=59)).isoformat()
    wt = make_task(c, issue=42, stage=main.Stage.AWAITING_PLAN_REVIEW,
                   operator_request=ARMED,
                   updated_at=just_under)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)

    main.run_pass(c, d)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.park == ""
    assert sess.ended == []
    assert "plan_parked" not in d.notifier.sent


def test_default_grace_parks_at_15_minutes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)  # default spec_review_grace_minutes == 15
    at_boundary = (datetime.now(timezone.utc)
                  - timedelta(minutes=15, seconds=1)).isoformat()
    wt = make_task(c, issue=42, stage=main.Stage.AWAITING_PLAN_REVIEW,
                   operator_request=ARMED,
                   updated_at=at_boundary)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)

    main.run_pass(c, d)

    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.park == PARK_REVIEW
    assert sess.ended == [42]
    assert "plan_parked" in d.notifier.sent
