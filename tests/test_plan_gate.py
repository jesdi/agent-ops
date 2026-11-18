import json
import os
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from telegram.inbound import Reply
from dispatcher.deps import Deps
from dispatcher import spec_publish
from dispatcher.github import Candidate
from dispatcher.state import (NO_SLOT, PlanApprovalRequest, Stage, load, load_all, save)
from tests.dispatchfakes import (GOOD_TICKET, PLAN_SUMMARY,
                                 SPEC_PATH, EveryRepoApp, FakeGitHub, FakeNotifier, FakeSessions,
                                 LiveUntilEnded, approve_plan, arm_gate, cfg, deps, gate_signal,
                                 make_task, patch_events, patch_usage,
                                 patch_workspace, record_session,
                                 replace_capacity, valid_spec, write_tickets,
                                 SPEC_URL, _plan_ready_task, parked)
from tests.statuses import held, review, failed
from dispatcher.status import Kind
from dispatcher.state import at_gate, operator_request
from dispatcher import eventlog
from dispatcher import intents as intents_mod


# A task that waits at the gate has its request armed; only then does the
# grace clock run.
ARMED = PlanApprovalRequest(".agent/review.html")


def test_gate_park_ends_session_and_frees_capacity_and_slot(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(ARMED), slot=1)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_REVIEW
    assert t.slot == NO_SLOT
    assert t.stage is Stage.PLAN   # stage preserved
    assert t.park_msg_id == 77                     # reply-to-wake repository
    assert sess.ended == [42]
    assert "plan_parked" in d.notifier.sent


def test_gate_park_is_event_logged(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(ARMED))
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir)
              if e["event"] == "parked"]
    assert len(parked) == 1
    assert parked[0]["issue"] == 42
    assert parked[0]["stage"] == "plan"
    assert parked[0]["detail"] == "plan review grace expired"


def test_gate_holds_inside_the_grace_period(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(PlanApprovalRequest(PLAN_SUMMARY)),
                   updated_at=fresh)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL and t.slot == 0
    assert sess.ended == []
    assert "plan_parked" not in d.notifier.sent


def test_gate_parked_task_does_not_block_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=77)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [99]


def test_zero_grace_parks_on_the_next_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(ARMED),
                   updated_at=datetime.now(timezone.utc).isoformat())
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_REVIEW


def test_grace_expiry_park_preserves_plan_approval_request(tmp_path, monkeypatch):
    """Slice 9 lock-in: _park_for_review's plain replace() at main.py:684
    does NOT touch operator_request or spec_path, so both survive the park.
    GET /request returns the plan-approval body even when park==PARK_REVIEW,
    proving request presence is independent of park==""."""
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS
    from web.app import create_app
    from web.sources import Sources
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="https://example.com/spec"))
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
                   spec_path="docs/specs/x-design.md")
    (wt / ".agent" / "review.html").write_text("<meta name=\"agent-ops-review\" content=\"1\">Approve me.")
    write_tickets(wt, 2)
    gate_signal(wt)  # status=awaiting-review, grace already elapsed → ParkForReview
    arm_gate(c)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_REVIEW
    assert operator_request(t) == PlanApprovalRequest(".agent/review.html"), "operator_request must survive park"
    assert t.spec_path == "docs/specs/x-design.md", "spec_path must survive park"
    # GET /request must serve the plan-approval body (the summary the request names)
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "plan-approval"
    assert body["content"]["kind"] == "readable"
    assert body["content"]["path"] == ".agent/review.html"
    assert body["content"]["text"] == "<meta name=\"agent-ops-review\" content=\"1\">Approve me."


def test_plan_parked_ping_links_spec(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, stage=Stage.PLAN, status=held(ARMED),
                   spec_path="docs/superpowers/specs/x-design.md")
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url=SPEC_URL))
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    (tmpl, ctx), = [x for x in d.notifier.contexts if x[0] == "plan_parked"]
    assert f"spec: {SPEC_URL}" in ctx["note"]


def test_overnight_drain_parks_every_ready_plan_then_one_reply_advances_one(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=2, spec_review_grace_minutes=0)
    gh = FakeGitHub([Candidate(n, f"task {n}", f"u{n}") for n in range(1, 6)])
    sess = LiveUntilEnded()
    d = deps(gh, sess)

    # Each pass: every spec session that has been spawned reports stage 1
    # done, every plan session reports its plan ready, then the dispatcher
    # runs. With grace 0 a task parks on the pass after its stage flips to
    # the gate, freeing capacity AND its slot for the next Ready candidate.
    for _ in range(24):
        for t in load_all(c.state_dir):
            wt = Path(t.worktree)
            if t.stage is Stage.SPEC and t.status.kind is Kind.RUNNING:
                valid_spec(wt)
                (wt / ".agent" / "stage.json").write_text(json.dumps(
                    {"stage": "spec", "status": "done", "note": "",
                     "artifact": "spec.md", "track": "standard"}))
            elif t.stage is Stage.PLAN and t.status.kind in (Kind.RUNNING,
                                                             Kind.HELD_PLAN_APPROVAL):
                write_tickets(wt, 1)
                gate_signal(wt)
        main.run_pass(c, d)

    tasks = load_all(c.state_dir)
    assert sorted(t.issue for t in tasks) == [1, 2, 3, 4, 5]
    assert all(t.status.kind is Kind.PARKED_REVIEW for t in tasks), \
        [(t.issue, t.stage.value, t.status.kind) for t in tasks]
    assert all(t.slot == NO_SLOT for t in tasks)
    assert sorted(gh.claimed) == [1, 2, 3, 4, 5]   # capacity 2 did NOT cap it
    for t in tasks:
        record_session(c, "plan", t.issue)

    # Morning: one reply resumes exactly one task, onto a real slot, and it
    # advances to IMPLEMENT once the session signals the approved plan done.
    patch_events(monkeypatch, [Reply(reply_to_msg_id=77, text="ship it")])
    main.run_pass(c, d)
    resumed = [t for t in load_all(c.state_dir) if t.status.kind is Kind.RUNNING]
    assert len(resumed) == 1
    woken = resumed[0]
    assert woken.slot in range(3)

    approve_plan(c, Path(woken.worktree), woken.spec_path)
    (Path(woken.worktree) / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "note": "", "artifact": ".agent/tickets"}))
    patch_events(monkeypatch, [])
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", woken.issue).stage is Stage.IMPLEMENT
    assert (woken.issue, "implement") in [(s[0], s[1]) for s in sess.spawned]


def test_park_for_review_saves_park_note(tmp_path, monkeypatch):
    # Same arrangement as test_gate_park_ends_session_and_frees_capacity_and_slot
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, status=held(ARMED), slot=1)
    write_tickets(wt, 2)
    gate_signal(wt)
    arm_gate(c)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).status.note == "plan ready for review"


# --- plan review gate, fix round 1 ------------------------------------------

def _gate_pass(c, sess, monkeypatch, **kw):
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **k: spec_publish.PublishResult(url=SPEC_URL))
    d = deps(sess=sess, **kw)
    main.run_pass(c, d)
    return d


def test_only_a_spec_done_or_an_approval_changes_the_track(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)            # on the standard track
    sig = wt / ".agent" / "stage.json"
    sess = FakeSessions(alive={42})
    for status in ("working", "awaiting-review"):
        sig.write_text(json.dumps({"stage": "plan", "status": status,
                                   "artifact": PLAN_SUMMARY, "track": "trivial"}))
        _gate_pass(c, sess, monkeypatch)
        assert load(c.state_dir, "portfolio_eval", 42).track == "standard", status
    assert at_gate(load(c.state_dir, "portfolio_eval", 42))
    approve_plan(c, wt, SPEC_PATH)
    sig.write_text(json.dumps({"stage": "plan", "status": "done", "track": "trivial"}))
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.track) == (Stage.IMPLEMENT, "trivial")


def test_premature_done_naming_a_track_does_not_change_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "track": "trivial"}))
    _gate_pass(c, FakeSessions(alive={42}), monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.track, t.plan_slips) == (Stage.PLAN, "standard", 1)


def test_ready_again_after_a_resume_is_a_new_review_round(tmp_path, monkeypatch):
    """After a resume cleared the request, a ready report publishes, notifies
    and restarts the grace clock; a repeat with the request armed is silent."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    wt = make_task(c, stage=Stage.PLAN, gated=True, spec_path=SPEC_PATH,
                   updated_at=stamp)
    write_tickets(wt, 2)
    gate_signal(wt)
    sess = FakeSessions(alive={42})
    seen = []
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **k: seen.append(k) or spec_publish.PublishResult(url=SPEC_URL))
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert t.updated_at > stamp, "a new round restarts the grace clock"
    assert len(seen) == 1 and d.notifier.sent.count("awaiting_plan_review") == 1
    main.run_pass(c, d)
    assert len(seen) == 1 and d.notifier.sent.count("awaiting_plan_review") == 1
    assert load(c.state_dir, "portfolio_eval", 42).updated_at == t.updated_at


def test_ready_again_with_a_ticket_gap_does_not_ask_for_review(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, gated=True, spec_path=SPEC_PATH,
                   updated_at=datetime.now(timezone.utc).isoformat())
    write_tickets(wt, 1)
    (wt / ".agent" / "tickets" / "03-late.md").write_text(GOOD_TICKET)
    gate_signal(wt)
    sess = FakeSessions(alive={42})
    d = _gate_pass(c, sess, monkeypatch)
    assert len(sess.resumed) == 1 and "contiguous" in sess.resumed[0][1]
    assert "awaiting_plan_review" not in d.notifier.sent
    assert operator_request(load(c.state_dir, "portfolio_eval", 42)) is None


def test_working_at_the_gate_clears_the_request_and_ready_is_a_new_round(
        tmp_path, monkeypatch):
    """Feedback through /attach: while the session reworks the plan the stale
    approve request is gone; its next ready report is announced again."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)
    armed = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(armed) is not None
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "working", "note": "applying feedback"}))
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t) and operator_request(t) is None
    gate_signal(wt)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert d.notifier.sent.count("awaiting_plan_review") == 1


def test_gate_phase_has_its_own_retry(tmp_path, monkeypatch):
    """Tickets invalid once → fixed → gate → approval with a misspelled track
    is bounced with the list, not parked."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c, plan_retries=1)   # the ticket retry was used
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)
    assert load(c.state_dir, "portfolio_eval", 42).plan_retries == 0
    approve_plan(c, wt, SPEC_PATH)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "track": "standrad"}))
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and len(sess.resumed) == 1
    assert "deep" in sess.resumed[0][1] and "standard" in sess.resumed[0][1]


def test_ticket_gap_after_a_premature_done_bounce_is_resumed_not_failed(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sig = wt / ".agent" / "stage.json"
    sig.write_text(json.dumps({"stage": "plan", "status": "done"}))
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)              # bounced: nobody approved
    (wt / ".agent" / "tickets" / "04-late.md").write_text(GOOD_TICKET)
    gate_signal(wt)
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.PLAN and t.status.kind is Kind.RUNNING
    assert len(sess.resumed) == 2 and "contiguous" in sess.resumed[1][1]


def test_task_failed_from_the_gate_drops_the_approval_request(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(PLAN_SUMMARY)), spec_path=SPEC_PATH,
                   plan_retries=1)
    (wt / ".agent" / "tickets").mkdir()
    (wt / ".agent" / "tickets" / "01-bad.md").write_text("# tiny\n")
    approve_plan(c, wt, SPEC_PATH)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done"}))
    _gate_pass(c, FakeSessions(alive={42}), monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED and operator_request(t) is None


def test_approval_sent_to_a_live_gate_reaches_the_session(tmp_path, monkeypatch):
    """A console reply to a gate task that has not parked yet must reach the
    session, also when the grace time ends before the session reacts."""
    from dispatcher import messages
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = LiveUntilEnded(alive={42})
    _gate_pass(c, sess, monkeypatch)
    assert at_gate(load(c.state_dir, "portfolio_eval", 42))
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                         {"text": "Approved — proceed."}, actor="op", epoch_ms=1)
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and t.slot != NO_SLOT and 42 in sess.alive_set
    # The grace time ends before the session reacts: nothing is lost,
    # and the session's `done` starts implement.
    save(c.state_dir, dc_replace(t, updated_at=(
        datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()))
    approve_plan(c, wt, SPEC_PATH)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done"}))
    _gate_pass(c, sess, monkeypatch)
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.IMPLEMENT
    got = [m for _i, m, *_ in sess.resumed if "Approved — proceed." in m]
    assert len(got) == 1, "the reply is delivered, and only once"
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def _signals_until_stopped(c, sess, monkeypatch, wt, signals, passes=8):
    """A session that never learns: each pass it writes the next of `signals`
    again. Returns the task once it parked or failed (or after `passes`)."""
    for i in range(passes):
        t = load(c.state_dir, "portfolio_eval", 42)
        if parked(t):
            break
        (wt / ".agent" / "stage.json").write_text(json.dumps(signals[i % len(signals)]))
        _gate_pass(c, sess, monkeypatch)
    return load(c.state_dir, "portfolio_eval", 42)


READY = {"stage": "plan", "status": "awaiting-review", "artifact": PLAN_SUMMARY}
WORKING = {"stage": "plan", "status": "working"}


@pytest.mark.parametrize("gate", [False, True], ids=["before-gate", "at-gate"])
@pytest.mark.parametrize("case, signals, resumes, end", [
    ("bad tickets", [WORKING, READY], 1, "failed"),
    ("bad track", [{"stage": "plan", "status": "done", "track": "nope"}], 1, "parked"),
    ("bad track after rework", [WORKING, READY,
                                {"stage": "plan", "status": "done", "track": "nope"}], 1, "parked"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_no_plan_signal_loop_is_resumed_more_than_once(
        tmp_path, monkeypatch, gate, case, signals, resumes, end):
    """Every bounce of the plan signal is bounded with no operator action: a
    round at the gate (ready again after `working`) never refills a retry."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    if gate:
        _gate_pass(c, sess, monkeypatch)
        assert at_gate(load(c.state_dir, "portfolio_eval", 42))
    if case == "bad tickets":
        (wt / ".agent" / "tickets" / "05-late.md").write_text(GOOD_TICKET)
    t = _signals_until_stopped(c, sess, monkeypatch, wt, signals)
    assert len(sess.resumed) == resumes
    if end == "failed":
        assert t.status.kind is Kind.FAILED
    else:
        assert t.status.kind is Kind.PARKED_HUMAN
    assert [s for s in sess.spawned if s[1] == "implement"] == []


# --- plan review gate, fix round 2 ------------------------------------------

def _minutes_ago(c, n):
    t = load(c.state_dir, "portfolio_eval", 42)
    save(c.state_dir, dc_replace(t, updated_at=(
        datetime.now(timezone.utc) - timedelta(minutes=n)).isoformat()))


def test_long_rework_with_a_ticket_gap_is_not_parked_for_review(tmp_path, monkeypatch):
    """Gate → working → 20 minutes → ready with tickets 01, 03, 04: the ticket
    check runs before anything reaches the operator, grace time or not."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)
    (wt / ".agent" / "stage.json").write_text(json.dumps(WORKING))
    _gate_pass(c, sess, monkeypatch)
    tickets = wt / ".agent" / "tickets"
    (tickets / "02-t2.md").rename(tickets / "03-t3.md")
    (tickets / "04-t4.md").write_text(GOOD_TICKET)
    _minutes_ago(c, 20)
    gate_signal(wt)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and operator_request(t) is None
    assert d.notifier.sent == ["plan_retry"]
    assert len(sess.resumed) == 1 and "contiguous" in sess.resumed[0][1]


def test_rework_longer_than_the_grace_time_starts_a_round_with_a_fresh_clock(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)
    (wt / ".agent" / "stage.json").write_text(json.dumps(WORKING))
    _gate_pass(c, sess, monkeypatch)
    _minutes_ago(c, 20)
    gate_signal(wt)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL and operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert d.notifier.sent == ["awaiting_plan_review"]
    _minutes_ago(c, 16)                       # one grace time later it parks
    _gate_pass(c, sess, monkeypatch)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_REVIEW


def test_a_session_that_dies_at_the_gate_gets_two_respawns_then_parks(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    notifier = FakeNotifier()
    for _life in range(5):
        if parked(load(c.state_dir, "portfolio_eval", 42)):
            break
        gate_signal(wt)
        sess.alive_set = {42}
        _gate_pass(c, sess, monkeypatch, notifier=notifier)   # reaches the gate
        sess.alive_set = set()
        _gate_pass(c, sess, monkeypatch, notifier=notifier)   # found dead
    t = load(c.state_dir, "portfolio_eval", 42)
    # The respawn at the gate and its first ready report count one round.
    assert [s[1] for s in sess.spawned] == ["plan", "plan"]
    assert t.status.kind is Kind.PARKED_REVIEW and t.slot == NO_SLOT
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert notifier.sent.count("plan_session_stopped") == 1
    assert "plan_parked" not in notifier.sent
    assert notifier.sent.count("awaiting_plan_review") == 3
    for _ in range(3):                         # parked: nothing more happens
        _gate_pass(c, sess, monkeypatch, notifier=notifier)
    assert len(sess.spawned) == 2 and notifier.sent.count("plan_session_stopped") == 1


def test_flapping_at_the_gate_gets_two_new_rounds_then_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    notifier = FakeNotifier()
    gh = FakeGitHub()
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **k: spec_publish.PublishResult(url=SPEC_URL))
    d = Deps(github=gh, sessions=sess, notifier=notifier, app=EveryRepoApp())
    main.run_pass(c, d)
    for _flap in range(10):
        if parked(load(c.state_dir, "portfolio_eval", 42)):
            break
        (wt / ".agent" / "stage.json").write_text(json.dumps(WORKING))
        main.run_pass(c, d)
        gate_signal(wt)
        main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert notifier.sent.count("awaiting_plan_review") == 1 + 2
    assert len(gh.comments) == 1 + 2
    assert t.status.kind is Kind.PARKED_REVIEW and t.slot == NO_SLOT
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert notifier.sent.count("plan_parked") == 1


def test_rounds_that_follow_an_operator_reply_are_never_capped(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = LiveUntilEnded(alive={42})
    notifier = FakeNotifier()
    _gate_pass(c, sess, monkeypatch, notifier=notifier)
    for n in range(1, 6):
        intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                                 {"text": f"feedback {n}"}, actor="op", epoch_ms=n)
        _gate_pass(c, sess, monkeypatch, notifier=notifier)   # delivered
        gate_signal(wt)
        _gate_pass(c, sess, monkeypatch, notifier=notifier)   # ready again
        t = load(c.state_dir, "portfolio_eval", 42)
        assert t.status.kind is Kind.HELD_PLAN_APPROVAL, n
        assert notifier.sent.count("awaiting_plan_review") == 1 + n
    assert "plan_parked" not in notifier.sent
    assert len(sess.resumed) == 5


def test_round_that_follows_an_operator_reply_is_not_an_unattended_one(
        tmp_path, monkeypatch):
    """After a reply the operator is involved: the round that answers them is
    theirs, the next two unprompted rounds are announced, the third parks."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = LiveUntilEnded(alive={42})
    notifier = FakeNotifier()
    _gate_pass(c, sess, monkeypatch, notifier=notifier)
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "drop ticket 2"}, actor="op", epoch_ms=1)
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # delivered
    gate_signal(wt)
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # the answer's round
    assert load(c.state_dir, "portfolio_eval", 42).unattended_rounds == 0
    for flap in range(1, 4):
        (wt / ".agent" / "stage.json").write_text(json.dumps(WORKING))
        _gate_pass(c, sess, monkeypatch, notifier=notifier)
        gate_signal(wt)
        _gate_pass(c, sess, monkeypatch, notifier=notifier)
        t = load(c.state_dir, "portfolio_eval", 42)
        assert (t.status.kind is Kind.PARKED_REVIEW) == (flap == 3), flap
    assert notifier.sent.count("awaiting_plan_review") == 1 + 1 + 2
    assert notifier.sent.count("plan_parked") == 1


def test_capped_respawn_park_does_not_arm_an_unchecked_summary(tmp_path, monkeypatch):
    """The session died in the middle of a rework (request disarmed) and the
    respawns are used up: the task parks for review (#132 ticket 05), but on
    a request armed for no revision, so nothing can be approved, because no
    ready report passed the ticket check."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, gated=True, spec_path=SPEC_PATH,
                   unattended_rounds=2)
    write_tickets(wt, 2)
    gate_signal(wt)                       # the dead session's old signal
    d = _gate_pass(c, FakeSessions(alive=set()), monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.slot) == (Kind.PARKED_REVIEW, NO_SLOT)
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY, fingerprint="")
    # No plan is ready here: the message says what happened, not "plan ready".
    ((template, ctx),) = d.notifier.contexts
    assert template == "plan_session_stopped" and ctx["rounds"] == 2
    assert t.status.note == "plan session stopped at the review gate"
    (parked,) = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]
    assert parked["detail"] == "plan session died at the gate; unattended rounds used up"


def test_old_ready_signal_is_never_read_after_a_respawn_or_a_resume(tmp_path, monkeypatch):
    """A fresh or resumed session starts from a `working` signal, so the
    previous session's `awaiting-review` cannot arm a request for a summary
    the new session has not written."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, gated=True, spec_path=SPEC_PATH)
    write_tickets(wt, 2)
    sig = wt / ".agent" / "stage.json"
    sess = LiveUntilEnded()
    notifier = FakeNotifier()
    gate_signal(wt)
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # dead: respawn
    assert json.loads(sig.read_text())["status"] == "working"
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # new session, no report yet
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t) and operator_request(t) is None
    gate_signal(wt)
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # its own ready report
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "drop ticket 2"}, actor="op", epoch_ms=1)
    _gate_pass(c, sess, monkeypatch, notifier=notifier)       # resumed with feedback
    assert json.loads(sig.read_text())["status"] == "working"
    _gate_pass(c, sess, monkeypatch, notifier=notifier)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t) and operator_request(t) is None
    assert notifier.sent.count("awaiting_plan_review") == 1


# --- plan review gate: an armed request never outlives a ticket change -------

def test_tickets_changed_between_two_passes_disarm_the_armed_request(tmp_path, monkeypatch):
    """Between two passes the session wrote `working`, broke the ticket set and
    reported ready again: no pass saw `working`, so the request is still armed.
    The ticket check runs all the same, and the grace park offers nothing."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)                    # armed
    tickets = wt / ".agent" / "tickets"
    (tickets / "02-t2.md").rename(tickets / "03-t3.md")
    _minutes_ago(c, 20)                                 # past the grace time
    gate_signal(wt)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and operator_request(t) is None
    assert d.notifier.sent == ["plan_retry"]
    assert len(sess.resumed) == 1 and "contiguous" in sess.resumed[0][1]


def test_ready_report_after_a_bounced_approval_is_checked_again(tmp_path, monkeypatch):
    """`done` at the gate with a ticket gap is bounced; the session answers
    `awaiting-review` with the gap still there: nothing stays armed, and the
    one retry is not refilled."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)                    # armed
    tickets = wt / ".agent" / "tickets"
    (tickets / "02-t2.md").rename(tickets / "03-t3.md")
    approve_plan(c, wt, SPEC_PATH)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done"}))
    _gate_pass(c, sess, monkeypatch)                    # bounced
    assert operator_request(load(c.state_dir, "portfolio_eval", 42)) is None
    gate_signal(wt)
    _minutes_ago(c, 20)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == failed("artifact") and operator_request(t) is None
    assert d.notifier.sent == ["artifact_failed"]
    assert len(sess.resumed) == 1
    assert [s for s in sess.spawned if s[1] == "implement"] == []


def test_denied_retry_does_not_leave_the_changed_tickets_armed(tmp_path, monkeypatch):
    """The ticket set changed under an armed request and the usage gate denies
    the resume this pass: the request is disarmed all the same, so the grace
    park cannot offer the invalid set."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    sess = FakeSessions(alive={42})
    _gate_pass(c, sess, monkeypatch)                    # armed
    tickets = wt / ".agent" / "tickets"
    (tickets / "02-t2.md").rename(tickets / "03-t3.md")
    _minutes_ago(c, 20)
    patch_usage(monkeypatch, util=0.95)                 # no launch is admitted
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert sess.resumed == [] and t.plan_retries == 0
    assert t.status.kind is Kind.RUNNING and operator_request(t) is None


# --- plan review gate: the armed request is bound to one plan revision -------

def _armed_gate(c, sess, monkeypatch):
    """A task that reached the gate through a pass: summary on disk, armed."""
    wt = _plan_ready_task(c)
    (wt / PLAN_SUMMARY).write_text("<meta name=\"agent-ops-review\" content=\"1\">first plan")
    _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t) and operator_request(t).fingerprint
    return wt


def _rework_between_passes(wt, name):
    """What `name` selects changes with no `working` signal that a pass sees."""
    if name == "summary":
        (wt / PLAN_SUMMARY).write_text("<meta name=\"agent-ops-review\" content=\"1\">second plan")
    elif name == "ticket":
        ticket = wt / ".agent" / "tickets" / "02-t2.md"
        ticket.write_text(ticket.read_text() + "\n- [ ] one more criterion\n")
    else:
        (wt / ".agent" / "plan-v2.md").write_text("<meta name=\"agent-ops-review\" content=\"1\">second plan")
    gate_signal(wt, ".agent/plan-v2.md" if name == "path" else PLAN_SUMMARY)


@pytest.mark.parametrize("changed", ["summary", "ticket", "path"])
def test_plan_changed_under_an_armed_request_is_a_new_review_round(
        tmp_path, monkeypatch, changed):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt = _armed_gate(c, sess, monkeypatch)
    before = load(c.state_dir, "portfolio_eval", 42)
    _minutes_ago(c, 20)                       # past the grace time
    _rework_between_passes(wt, changed)
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert d.notifier.sent == ["awaiting_plan_review"]
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL and sess.ended == [], (
        "a new round, not the grace park")
    assert t.unattended_rounds == before.unattended_rounds + 1
    assert operator_request(t).fingerprint != operator_request(before).fingerprint
    assert operator_request(t).path == (".agent/plan-v2.md" if changed == "path"
                                       else PLAN_SUMMARY)
    assert _gate_pass(c, sess, monkeypatch).notifier.sent == []


def test_unchanged_plan_under_an_armed_request_is_never_announced_again(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt = _armed_gate(c, sess, monkeypatch)
    before = load(c.state_dir, "portfolio_eval", 42)
    for _ in range(3):
        gate_signal(wt)                       # the same report, written again
        assert _gate_pass(c, sess, monkeypatch).notifier.sent == []
    assert load(c.state_dir, "portfolio_eval", 42) == before


def test_armed_request_with_no_fingerprint_is_a_new_review_round(tmp_path, monkeypatch):
    """Fail closed: nothing says which plan such a request was armed for."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(PLAN_SUMMARY)), spec_path=SPEC_PATH)
    write_tickets(wt, 2)
    gate_signal(wt)
    d = _gate_pass(c, FakeSessions(alive={42}), monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert d.notifier.sent == ["awaiting_plan_review"]
    assert t.status.kind is Kind.HELD_PLAN_APPROVAL and operator_request(t).fingerprint


def test_plan_that_changes_every_pass_gets_two_new_rounds_then_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt = _armed_gate(c, sess, monkeypatch)
    notifier = FakeNotifier()
    for n in range(8):
        if parked(load(c.state_dir, "portfolio_eval", 42)):
            break
        (wt / PLAN_SUMMARY).write_text(f"<meta name=\"agent-ops-review\" content=\"1\">plan {n}")
        _gate_pass(c, sess, monkeypatch, notifier=notifier)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert notifier.sent == ["awaiting_plan_review"] * 2 + ["plan_parked"]
    assert t.status.kind is Kind.PARKED_REVIEW and operator_request(t).path == PLAN_SUMMARY


def test_fifo_at_the_stage_signal_path_does_not_stop_the_pass(tmp_path, monkeypatch):
    """stage.json is session-written: a FIFO there must not block the pass
    for this task, nor for the one behind it."""
    import threading
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN)
    os.mkfifo(wt / ".agent" / "stage.json")
    wt2 = make_task(c, issue=43, stage=Stage.IMPLEMENT, slot=1)
    (wt2 / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done"}))
    sess = FakeSessions(alive={42, 43})
    th = threading.Thread(target=lambda: main.run_pass(c, deps(sess=sess)), daemon=True)
    th.start()
    th.join(10)
    assert not th.is_alive(), "the pass blocks on the FIFO"
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PLAN
    assert load(c.state_dir, "portfolio_eval", 43).stage is Stage.REVIEW


# --- the plan revision is read defensively, and covers the spec folder -------

def _pass_within(c, d, seconds=5):
    import threading
    th = threading.Thread(target=lambda: main.run_pass(c, d), daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "the pass blocks"


def _assert_disarmed_and_resumed_once(c, sess, d):
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING
    assert operator_request(t) is None
    assert len(sess.resumed) == 1 and d.notifier.sent == ["plan_retry"]


def _plant_fifo_summary(wt, tmp_path):
    (wt / PLAN_SUMMARY).unlink()
    os.mkfifo(wt / PLAN_SUMMARY)


def _plant_fifo_ticket(wt, tmp_path):
    (wt / ".agent" / "tickets" / "02-t2.md").unlink()
    os.mkfifo(wt / ".agent" / "tickets" / "02-t2.md")


def _plant_symlinked_ticket(wt, tmp_path):
    ticket = wt / ".agent" / "tickets" / "02-t2.md"
    (tmp_path / "outside-ticket.md").write_text(ticket.read_text())
    ticket.unlink()
    ticket.symlink_to(tmp_path / "outside-ticket.md")


def _plant_symlinked_summary(wt, tmp_path):
    (tmp_path / "outside-page.html").write_text((wt / PLAN_SUMMARY).read_text())
    (wt / PLAN_SUMMARY).unlink()
    (wt / PLAN_SUMMARY).symlink_to(tmp_path / "outside-page.html")


@pytest.mark.parametrize("plant", [_plant_fifo_summary, _plant_fifo_ticket,
                                   _plant_symlinked_ticket, _plant_symlinked_summary],
                         ids=["fifo-summary", "fifo-ticket", "symlinked-ticket",
                              "symlinked-summary"])
def test_plan_file_that_is_no_regular_file_disarms_and_resumes_the_session(
        tmp_path, monkeypatch, plant):
    """Never a blocked pass, never an exception, never an approval left armed."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt = _armed_gate(c, sess, monkeypatch)
    plant(wt, tmp_path)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **k: spec_publish.PublishResult(url=SPEC_URL))
    d = deps(sess=sess)
    _pass_within(c, d)
    _assert_disarmed_and_resumed_once(c, sess, d)
    assert "regular file" in sess.resumed[0][1] or "contiguous" in sess.resumed[0][1]


def test_plan_file_that_cannot_be_read_this_pass_does_not_fail_the_task(
        tmp_path, monkeypatch):
    """The summary went away between the listing and the read."""
    from dispatcher import artifacts
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    _armed_gate(c, sess, monkeypatch)
    real, calls = artifacts.read_regular, []

    def once(path, max_bytes):
        if str(path).endswith("review.html") and not calls:
            calls.append(path)
            raise FileNotFoundError(path)
        return real(path, max_bytes)
    monkeypatch.setattr(artifacts, "read_regular", once)
    d = _gate_pass(c, sess, monkeypatch)
    assert calls
    _assert_disarmed_and_resumed_once(c, sess, d)


def _armed_gate_with_spec_folder(c, sess, monkeypatch):
    wt = _plan_ready_task(c)
    folder = wt / Path(SPEC_PATH).parent
    folder.mkdir(parents=True)
    for name in ("proposal.md", "spec.md", "design.md"):
        (folder / name).write_text(f"# {name}\n\nfirst\n")
    (wt / PLAN_SUMMARY).write_text("<meta name=\"agent-ops-review\" content=\"1\">first plan")
    _gate_pass(c, sess, monkeypatch)
    return wt, folder


@pytest.mark.parametrize("name", ["design.md", "spec.md", "proposal.md"])
def test_spec_folder_file_changed_under_an_armed_request_is_a_new_review_round(
        tmp_path, monkeypatch, name):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt, folder = _armed_gate_with_spec_folder(c, sess, monkeypatch)
    before = operator_request(load(c.state_dir, "portfolio_eval", 42)).fingerprint
    assert _gate_pass(c, sess, monkeypatch).notifier.sent == []      # unchanged: silent
    (folder / name).write_text(f"# {name}\n\nsecond\n")
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert d.notifier.sent == ["awaiting_plan_review"]
    assert operator_request(t).fingerprint not in ("", before)


def test_spec_folder_that_leaves_the_worktree_is_never_armed(tmp_path, monkeypatch):
    """A spec folder swapped for a symlink out of the worktree cannot be
    read as part of the revision: the approval is disarmed, never kept for a
    plan whose spec files are no longer covered."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt, folder = _armed_gate_with_spec_folder(c, sess, monkeypatch)
    outside = tmp_path / "outside"
    folder.rename(outside)
    folder.symlink_to(outside, target_is_directory=True)
    _gate_pass(c, sess, monkeypatch)
    assert operator_request(load(c.state_dir, "portfolio_eval", 42)) is None


def test_same_summary_bytes_at_another_path_is_a_new_review_round(tmp_path, monkeypatch):
    """The request names a path: the console reads that file. The same text
    in another file is not what the operator was shown."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    sess = FakeSessions(alive={42})
    wt = _armed_gate(c, sess, monkeypatch)
    other = wt / ".agent" / "v2" / "review.html"
    other.parent.mkdir()
    other.write_bytes((wt / PLAN_SUMMARY).read_bytes())
    gate_signal(wt, ".agent/v2/review.html")
    d = _gate_pass(c, sess, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert d.notifier.sent == ["awaiting_plan_review"]
    assert operator_request(t).path == ".agent/v2/review.html"
