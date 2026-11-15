"""Characterization: the rules a pass applies to a driven task's session
(machine.next_actions and pass_actions before #132 moved them into the
lifecycle table). Driven through run_pass and observed through the task
file, events.jsonl and the fakes only, so the tests outlive the move."""
import json
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.main as main
from dispatcher import eventlog, spec_publish
from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_REVIEW,
                              PlanApprovalRequest, Stage, active, load, load_all,
                              mark_background, mark_waiting, save)
from dispatcher.artifacts import plan_revision
from tests.test_herdr_liveness_poke_acceptance import HerdrSessions
from tests.test_main import (PLAN_SUMMARY, SPEC_PATH, FakeGitHub, FakeSessions,
                             approve_plan, cfg, deps, make_task, patch_usage,
                             patch_workspace, write_tickets)

T, N = "portfolio_eval", 42
MIN, HOUR = 60, 3600
NOW = datetime.now(timezone.utc).isoformat()
GOOD_SPEC = ("# t — design\n\n## Problem\n\n" + "x " * 400
             + "\n\n## Decisions\n\n" + "y " * 400)
STALL_NOTE = "(no session output for 10m — likely login/trust prompt or hang)"
CAP_NOTE = "(background work still running after 180m — cap reached)"
LOGIN_TAIL = ("Browser didn't open? Use the url below to sign in:\n\n"
              "https://claude.ai/oauth/authorize?code=true&client_id=x\n\n"
              "Paste code here if prompted >\n")
WORK = {"id": "b1", "type": "shell", "command": "make crap-gate"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="https://x/spec"))
    return cfg(tmp_path)


def task(c, stage=Stage.IMPLEMENT, **kw):
    kw.setdefault("updated_at", NOW)
    return make_task(c, issue=N, stage=stage, **kw)


def signal(wt, status, stage="implement", **extra):
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": stage, "status": status, **extra}))


def run(c, sess=None, gh=None, passes=1):
    d = deps(gh or FakeGitHub(), sess if sess is not None else FakeSessions(alive={N}))
    for _ in range(passes):
        main.run_pass(c, d)
    return d


def now(c):
    return load(c.state_dir, T, N)


def events(c, name):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == name]


def stopped(**kw):
    """A live session herdr sees stopped at its composer."""
    return HerdrSessions({N: ("idle", 5)}, alive=[N], idle={N: 30.0}, **kw)


# --- precedence: track, background wait, death, stall, signal ----------------

def test_a_task_whose_track_left_the_config_parks_for_config(c):
    wt = task(c, track="renamed")
    signal(wt, "done")
    d = run(c)
    t = now(c)
    assert (t.park, t.park_reason, t.slot) == (PARK_HUMAN, "config", NO_SLOT)
    assert "'renamed'" in t.park_note and N in d.sessions.ended
    assert "parked_question" in d.notifier.sent and d.sessions.spawned == []


def test_a_dead_session_without_done_crashes_and_keeps_its_stage(c):
    task(c)
    d = run(c, FakeSessions(alive=set()))
    t = now(c)
    assert (t.stage, t.crashed_stage, t.park) == (Stage.FAILED, "implement", "")
    assert d.github.released == [(N, "session crashed mid-stage")]
    assert "session_crashed" in d.notifier.sent and N in d.sessions.ended


def test_a_dead_session_with_a_done_signal_takes_the_done_path(c):
    wt = task(c)
    signal(wt, "done")
    d = run(c, FakeSessions(alive=set()))
    assert [s[:2] for s in d.sessions.spawned] == [(N, "review")]
    assert now(c).stage is Stage.REVIEW


def test_a_dead_session_at_the_gate_respawns_plan_while_rounds_remain(c):
    task(c, stage=Stage.AWAITING_PLAN_REVIEW, spec_path=SPEC_PATH,
         operator_request=PlanApprovalRequest(PLAN_SUMMARY))
    d = run(c, FakeSessions(alive=set()))
    assert [s[:2] for s in d.sessions.spawned] == [(N, "plan")]
    t = now(c)
    assert (t.stage, t.operator_request, t.gate_respawn_pending) == (
        Stage.AWAITING_PLAN_REVIEW, None, True)


def test_a_dead_session_at_the_gate_parks_for_review_with_no_rounds_left(c):
    task(c, stage=Stage.AWAITING_PLAN_REVIEW, spec_path=SPEC_PATH, unattended_rounds=2,
         operator_request=PlanApprovalRequest(PLAN_SUMMARY, fingerprint="fp"))
    d = run(c, FakeSessions(alive=set()))
    t = now(c)
    assert (t.park, t.slot, t.park_note) == (
        PARK_REVIEW, NO_SLOT, "plan session stopped at the review gate")
    assert t.operator_request == PlanApprovalRequest(PLAN_SUMMARY, fingerprint="fp")
    assert "plan_session_stopped" in d.notifier.sent and d.sessions.spawned == []


def test_a_claim_that_never_launched_spawns_spec(c):
    task(c, stage=Stage.QUEUED, unlaunched=True)
    d = run(c, FakeSessions(alive=set()))
    assert [s[:2] for s in d.sessions.spawned] == [(N, "spec")]
    t = now(c)
    assert (t.stage, t.unlaunched, t.slot) == (Stage.SPEC, False, 0)


@pytest.mark.parametrize("sig", [None, {"status": "working"}])
def test_a_silent_session_past_the_stall_time_parks_stalled(c, sig):
    wt = task(c)
    if sig:
        signal(wt, sig["status"])
    d = run(c, FakeSessions(alive={N}, idle={N: 601.0}))
    t = now(c)
    assert (t.park, t.park_reason, t.park_note, t.slot) == (
        PARK_HUMAN, "stalled", STALL_NOTE, NO_SLOT)
    assert N in d.sessions.ended


@pytest.mark.parametrize("idle, stall_after", [(600.0, 600), (599.0, 600), (None, 600),
                                               (5000.0, 0)])
def test_a_session_inside_the_stall_time_or_with_detection_off_is_left_alone(
        c, idle, stall_after):
    c = dc_replace(c, stall_after_seconds=stall_after)
    task(c)
    before = now(c)
    run(c, FakeSessions(alive={N}, idle={N: idle} if idle is not None else {}))
    assert now(c) == before


def test_a_session_at_the_gate_waiting_for_review_never_stalls(c):
    wt = task(c, stage=Stage.PLAN, spec_path=SPEC_PATH)
    write_tickets(wt, 2)
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY)
    run(c, FakeSessions(alive={N}))
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY)
    d = run(c, FakeSessions(alive={N}, idle={N: 5000.0}))
    t = now(c)
    assert (t.stage, t.park) == (Stage.AWAITING_PLAN_REVIEW, "")
    assert d.notifier.sent == []


def test_a_live_session_with_no_signal_changes_nothing(c):
    task(c)
    before = now(c)
    d = run(c)
    assert now(c) == before and d.notifier.sent == [] and d.sessions.ended == []


def test_an_unknown_signal_status_changes_nothing(c):
    wt = task(c)
    signal(wt, "pondering")
    before = now(c)
    run(c)
    assert now(c) == before


# --- signals -------------------------------------------------------------------

def test_awaiting_ci_parks_on_the_run_and_ends_the_session(c):
    wt = task(c)
    signal(wt, "awaiting-ci", run_id=991)
    d = run(c)
    t = now(c)
    assert (t.park, t.ci_run_id, t.slot) == (PARK_CI, 991, NO_SLOT)
    assert N in d.sessions.ended


def test_awaiting_ci_without_a_run_id_fails_the_task(c):
    wt = task(c)
    signal(wt, "awaiting-ci")
    d = run(c)
    assert now(c).stage is Stage.FAILED
    assert ("artifact_failed", "awaiting-ci without run_id") in [
        (t, ctx["note"]) for t, ctx in d.notifier.contexts]


def test_parking_releases_the_slot(c):
    wt = task(c, slot=3)
    signal(wt, "blocked", note="which DB?")
    d = run(c)
    t = now(c)
    assert (t.park, t.park_reason, t.park_note, t.slot) == (
        PARK_HUMAN, "decision", "which DB?", NO_SLOT)
    assert active(load_all(c.state_dir)) == []
    assert N in d.sessions.ended and "parked_question" in d.notifier.sent
    assert [e["detail"] for e in events(c, "parked")] == ["which DB?"]


def test_awaiting_answers_parks_with_the_page_as_the_request(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / ".agent" / "questions.html").write_text("<p>q</p>")
    signal(wt, "awaiting-answers", stage="spec", artifact=".agent/questions.html",
           note="pick one")
    run(c)
    t = now(c)
    assert (t.park, t.park_note, t.operator_request.path, t.asked) == (
        PARK_HUMAN, "pick one", ".agent/questions.html", True)


def test_awaiting_answers_with_an_unusable_page_parks_with_a_note(c):
    wt = task(c)
    signal(wt, "awaiting-answers", artifact="/etc/passwd", note="pick one")
    run(c)
    t = now(c)
    assert t.park == PARK_HUMAN and t.operator_request is None
    assert "malformed awaiting-answers" in t.park_note


def test_a_park_at_a_login_prompt_holds_the_session_and_its_slot(c):
    wt = task(c, stage=Stage.SPEC, slot=1)
    signal(wt, "blocked", stage="spec", note="stuck")
    sess = FakeSessions(alive={N}, tail=LOGIN_TAIL)
    d = run(c, sess)
    t = now(c)
    assert (t.park, t.slot) == (PARK_LOGIN, 1)
    assert active(load_all(c.state_dir)) == [t]
    assert N not in d.sessions.ended and "needs_relogin" in d.notifier.sent


def test_a_new_loop_round_is_counted(c):
    wt = task(c, stage=Stage.REVIEW)
    signal(wt, "working", stage="review", loop="review", round=1)
    d = run(c)
    t = now(c)
    assert (t.review_rounds, t.park) == (1, "")
    assert [e["detail"] for e in events(c, "round")] == ["review round 1/2"]
    assert d.notifier.sent == []


def test_the_last_loop_round_pings(c):
    wt = task(c, stage=Stage.REVIEW, review_rounds=1)
    signal(wt, "working", stage="review", loop="review", round=2)
    d = run(c)
    assert now(c).review_rounds == 2 and "last_round" in d.notifier.sent


def test_a_loop_round_past_its_cap_parks_for_the_operator(c):
    wt = task(c, stage=Stage.REVIEW, review_rounds=2)
    signal(wt, "working", stage="review", loop="review", round=3)
    d = run(c)
    t = now(c)
    assert (t.park, t.park_reason, t.slot) == (PARK_HUMAN, "loop cap", NO_SLOT)
    assert "review loop exceeded its cap of 2 rounds" in t.park_note
    assert N in d.sessions.ended


@pytest.mark.parametrize("extra", [dict(loop="nonsense", round=4), dict(loop="review")])
def test_a_round_report_the_policy_does_not_count_changes_nothing(c, extra):
    wt = task(c, stage=Stage.REVIEW)
    signal(wt, "working", stage="review", **extra)
    before = now(c)
    run(c)
    assert now(c) == before


def test_a_working_report_at_the_gate_disarms_the_request(c):
    wt = task(c, stage=Stage.AWAITING_PLAN_REVIEW, spec_path=SPEC_PATH,
              operator_request=PlanApprovalRequest(PLAN_SUMMARY, fingerprint="fp"))
    signal(wt, "working", stage="plan")
    run(c)
    t = now(c)
    assert (t.stage, t.operator_request, t.park) == (Stage.AWAITING_PLAN_REVIEW, None, "")


# --- pokes ------------------------------------------------------------------

def test_a_stopped_session_is_poked_then_parked(c):
    c = dc_replace(c, background_wait_seconds=2 * HOUR)
    wt = task(c)
    signal(wt, "working")
    sess = stopped()
    for _ in range(2):
        mark_waiting(c.state_dir, T, N)
        run(c, sess)
    t = now(c)
    assert (t.park, t.pokes) == ("", 2) and len(sess.sent_text) == 2
    assert [e["detail"] for e in events(c, "poked")] == ["poke 1", "poke 2"]
    save(c.state_dir, dc_replace(t, poked_since=time.time() - 2 * HOUR - 1))
    mark_waiting(c.state_dir, T, N)
    run(c, sess)
    t = now(c)
    assert (t.park, t.park_reason) == (PARK_HUMAN, "stalled")
    assert "poked 2 times over 120m" in t.park_note and len(sess.sent_text) == 2


def test_a_poke_leaves_updated_at_alone(c):
    wt = task(c)
    signal(wt, "working")
    mark_waiting(c.state_dir, T, N)
    before = now(c)
    run(c, stopped())
    assert now(c).updated_at == before.updated_at and now(c).pokes == 1


def test_a_stopped_session_herdr_cannot_see_is_not_poked(c):
    wt = task(c)
    signal(wt, "working")
    mark_waiting(c.state_dir, T, N)
    sess = FakeSessions(alive={N}, idle={N: 30.0})   # agent_state None
    run(c, sess)
    assert sess.sent_text == [] and now(c).park == ""


def test_a_stopped_session_at_a_prompt_parks_instead_of_a_poke(c):
    wt = task(c)
    signal(wt, "working")
    mark_waiting(c.state_dir, T, N)
    sess = stopped()
    sess.state[N] = ("blocked", 5)
    run(c, sess)
    t = now(c)
    assert sess.sent_text == [] and t.park == PARK_HUMAN
    assert t.park_note == "(session stopped mid-stage waiting for input)"


def test_a_session_herdr_sees_working_ends_the_poke_stretch(c):
    wt = task(c, poked_since=time.time() - 60, pokes=1)
    signal(wt, "working")
    run(c, HerdrSessions({N: ("working", 6)}, alive=[N], idle={N: 0.0}))
    t = now(c)
    assert (t.poked_since, t.pokes, t.park) == (0.0, 0, "")


def test_a_round_report_with_a_waiting_marker_counts_and_pokes(c):
    wt = task(c, stage=Stage.REVIEW)
    signal(wt, "working", stage="review", loop="review", round=1)
    mark_waiting(c.state_dir, T, N)
    sess = stopped()
    run(c, sess)
    assert now(c).review_rounds == 1 and len(sess.sent_text) == 1


# --- background waits ----------------------------------------------------------

def _waiting_in_background(c, since_ago, recorded=True, agent=("idle", 5)):
    wt = task(c, stage=Stage.REVIEW)
    signal(wt, "working", stage="review")
    mark_background(c.state_dir, T, N, [WORK], now=time.time() - since_ago)
    sess = HerdrSessions({N: agent}, alive=[N], idle={N: 11 * MIN})
    if recorded:
        run(c, sess)   # the first pass records the wait
    return wt, sess


def test_a_background_report_is_recorded_and_leaves_updated_at_alone(c):
    _, sess = _waiting_in_background(c, 60, recorded=False)
    before = now(c)
    run(c, sess)
    t = now(c)
    assert t.background_reported and t.background_seq == 5
    assert (t.park, t.updated_at) == ("", before.updated_at)


def test_a_background_report_herdr_sees_working_is_not_recorded(c):
    _, sess = _waiting_in_background(c, 60, recorded=False, agent=("working", 5))
    run(c, sess)
    assert now(c).background_reported == 0.0


def test_a_recorded_background_wait_holds_off_the_stall(c):
    _, sess = _waiting_in_background(c, 60)
    run(c, sess)
    assert now(c).park == ""


def test_a_background_wait_past_its_cap_parks_stalled(c):
    _, sess = _waiting_in_background(c, 4 * HOUR)
    run(c, sess)
    t = now(c)
    assert (t.park, t.park_reason, t.park_note) == (PARK_HUMAN, "stalled", CAP_NOTE)


def test_a_new_turn_ends_the_background_wait_and_the_stall_applies(c):
    _, sess = _waiting_in_background(c, 60)
    sess.state[N] = ("idle", 6)
    run(c, sess)
    assert (now(c).park, now(c).park_note) == (PARK_HUMAN, STALL_NOTE)


def test_a_waiting_marker_takes_precedence_over_a_background_wait(c):
    _, sess = _waiting_in_background(c, 4 * HOUR)
    mark_waiting(c.state_dir, T, N)
    run(c, sess)
    t = now(c)
    assert t.park == "" and t.pokes == 1


def test_a_background_wait_still_counts_loop_rounds(c):
    wt, sess = _waiting_in_background(c, 60)
    signal(wt, "working", stage="review", loop="review", round=1)
    run(c, sess)
    assert (now(c).review_rounds, now(c).park) == (1, "")


# --- ready reports -------------------------------------------------------------

def _plan_ready(c, **kw):
    wt = task(c, stage=Stage.PLAN, spec_path=SPEC_PATH, **kw)
    write_tickets(wt, 2)
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY, note="plan ready")
    return wt


def test_a_ready_plan_enters_the_gate_and_keeps_its_slot(c):
    _plan_ready(c)
    d = run(c)
    t = now(c)
    assert (t.stage, t.park, t.slot, t.gated) == (Stage.AWAITING_PLAN_REVIEW, "", 0, True)
    assert t.operator_request.path == PLAN_SUMMARY and t.operator_request.fingerprint
    assert "awaiting_plan_review" in d.notifier.sent
    assert d.github.comments == [(N, "📝 Plan ready for review: https://x/spec")]


def test_a_ready_plan_with_bad_tickets_retries_then_fails(c):
    wt = _plan_ready(c)
    (wt / ".agent" / "tickets" / "01-t1.md").write_text("# tiny\n")
    d = run(c)
    assert (now(c).stage, now(c).plan_retries) == (Stage.PLAN, 1)
    assert len(d.sessions.resumed) == 1 and "plan_retry" in d.notifier.sent
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY)
    d = run(c)
    assert now(c).stage is Stage.FAILED and "artifact_failed" in d.notifier.sent


def test_a_ready_plan_on_a_gate_free_track_skips_the_gate(c):
    c = dc_replace(c, models=dc_replace(c.models, tracks={
        **c.models.tracks,
        "trivial": dc_replace(c.models.tracks["trivial"], plan_review=False)}))
    wt = _plan_ready(c, track="trivial")
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY, open_questions=0)
    d = run(c)
    t = now(c)
    assert (t.stage, t.gated, t.operator_request, t.ticket_count) == (
        Stage.IMPLEMENT, False, None, 2)
    assert [s[:2] for s in d.sessions.spawned] == [(N, "implement")]
    assert "implement_started" in d.notifier.sent
    assert "awaiting_plan_review" not in d.notifier.sent


def _at_the_gate(c, updated_at=NOW, **kw):
    wt = task(c, stage=Stage.AWAITING_PLAN_REVIEW, spec_path=SPEC_PATH, gated=True,
              updated_at=updated_at, **kw)
    write_tickets(wt, 2)
    rev = plan_revision(wt, PLAN_SUMMARY, SPEC_PATH)
    t = now(c)
    save(c.state_dir, dc_replace(t, operator_request=PlanApprovalRequest(
        rev.path, fingerprint=rev.fingerprint)))
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY)
    return wt


def test_an_armed_plan_inside_the_grace_time_waits(c):
    _at_the_gate(c)
    before = now(c)
    d = run(c)
    assert now(c) == before and d.notifier.sent == []


def test_an_armed_plan_past_the_grace_time_parks_for_review(c):
    old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    _at_the_gate(c, updated_at=old)
    request = now(c).operator_request
    d = run(c)
    t = now(c)
    assert (t.park, t.slot, t.operator_request) == (PARK_REVIEW, NO_SLOT, request)
    assert "plan_parked" in d.notifier.sent and N in d.sessions.ended


def test_a_ready_report_at_the_gate_with_no_request_is_a_new_round(c):
    wt = _at_the_gate(c)
    save(c.state_dir, dc_replace(now(c), operator_request=None))
    d = run(c)
    t = now(c)
    assert t.operator_request is not None and t.unattended_rounds == 1
    assert "awaiting_plan_review" in d.notifier.sent


def test_a_new_round_with_the_unattended_rounds_used_parks_for_review(c):
    _at_the_gate(c, unattended_rounds=2)
    save(c.state_dir, dc_replace(now(c), operator_request=None))
    d = run(c)
    t = now(c)
    assert (t.park, t.operator_request.path) == (PARK_REVIEW, PLAN_SUMMARY)
    assert "plan_parked" in d.notifier.sent


def test_a_ready_report_from_the_spec_stage_is_bounced_then_parked(c):
    wt = task(c, stage=Stage.SPEC)
    signal(wt, "awaiting-review", stage="spec")
    d = run(c)
    assert (now(c).spec_retries, now(c).park) == (1, "") and len(d.sessions.resumed) == 1
    signal(wt, "awaiting-review", stage="spec")
    run(c)
    assert (now(c).park, now(c).park_reason) == (PARK_HUMAN, "bad signal")


def test_a_ready_report_from_a_stage_with_no_gate_changes_nothing(c):
    wt = task(c)
    signal(wt, "awaiting-review")
    before = now(c)
    run(c)
    assert now(c) == before


@pytest.mark.parametrize("status", ["blocked", "done"])
def test_a_live_session_at_the_legacy_blocked_stage_is_left_alone(c, status):
    """The escalate-in-place stage: its session waits for the operator."""
    signal(task(c, stage=Stage.BLOCKED), status, stage="blocked", note="q")
    before = now(c)
    d = run(c)
    assert now(c) == before
    assert (d.notifier.sent, d.sessions.ended, d.sessions.spawned) == ([], [], [])


def test_done_at_a_stage_no_stage_follows_changes_nothing(c):
    signal(task(c, stage=Stage.STALLED_ON_BUDGET), "done", stage="stalled-on-budget")
    before = now(c)
    d = run(c)
    assert now(c) == before
    assert (d.notifier.sent, d.sessions.ended, d.sessions.spawned) == ([], [], [])


# --- done ----------------------------------------------------------------------

def test_implement_done_starts_review(c):
    wt = task(c, picks={"implement": "anthropic/claude-opus-5"},
              implement_providers=["anthropic"])
    signal(wt, "done", note="all tickets")
    d = run(c)
    assert [s[:2] for s in d.sessions.spawned] == [(N, "review")]
    assert ("review_started", "all tickets") in [
        (t, ctx["note"]) for t, ctx in d.notifier.contexts]
    assert now(c).stage is Stage.REVIEW


def test_implement_done_with_review_denied_waits_without_its_implement_pick(c, monkeypatch):
    wt = task(c, picks={"implement": "anthropic/claude-opus-5"},
              implement_providers=["anthropic"])
    signal(wt, "done")
    patch_usage(monkeypatch, util=0.99)
    d = run(c)
    t = now(c)
    assert d.sessions.spawned == [] and "implement" not in t.picks
    assert (t.stage, t.park) == (Stage.IMPLEMENT, "")


def test_a_launch_the_config_strands_parks_for_config(c):
    tracks = {k: v for k, v in c.models.tracks.items() if k != "deep"}  # no openai entry
    c = dc_replace(c, models=dc_replace(c.models, tracks=tracks))
    wt = task(c, picks={"implement": "anthropic/claude-opus-5",
                        "review": "openai/gpt-luna@high"})
    signal(wt, "done")
    d = run(c)
    t = now(c)
    assert (t.park, t.park_reason) == (PARK_HUMAN, "config")
    assert "runs on openai, which no model entry names" in t.park_note
    assert d.sessions.spawned == []


@pytest.mark.parametrize("stage, template", [(Stage.REVIEW, "pr_opened"),
                                             (Stage.ADDRESS_REVIEW, "pr_updated")])
def test_review_done_opens_the_pr(c, stage, template):
    wt = task(c, stage=stage)
    signal(wt, "done", stage=stage.value,
           artifact="https://github.com/jesdi/portfolio_eval/pull/88")
    d = run(c)
    t = now(c)
    assert (t.stage, t.pr_number, t.park) == (Stage.PR_OPEN, 88, "")
    assert template in d.notifier.sent
    assert len(events(c, "pr-opened")) == 1


def test_an_unapproved_plan_done_is_bounced_then_parked(c):
    wt = task(c, stage=Stage.PLAN)
    signal(wt, "done", stage="plan")
    d = run(c)
    assert (now(c).plan_slips, now(c).park) == (1, "") and len(d.sessions.resumed) == 1
    assert "accepted only after the operator has approved" in d.sessions.resumed[0][1]
    signal(wt, "done", stage="plan")
    run(c)
    assert (now(c).park, now(c).park_reason) == (PARK_HUMAN, "bad signal")


def test_a_gated_plan_done_is_bounced_like_any_unapproved_done(c):
    """#132 ticket 05 deleted the "gate state was lost" park: `gated` on a
    plan-stage task no longer changes what its `done` gets."""
    wt = task(c, stage=Stage.PLAN, gated=True)
    signal(wt, "done", stage="plan")
    d = run(c)
    assert (now(c).plan_slips, now(c).park) == (1, "") and len(d.sessions.resumed) == 1
    assert "accepted only after the operator has approved" in d.sessions.resumed[0][1]


def test_an_approved_plan_starts_one_implement_session(c):
    wt = _at_the_gate(c)
    approve_plan(c, wt, SPEC_PATH)
    signal(wt, "done", stage="plan")
    d = run(c)
    t = now(c)
    assert (t.stage, t.ticket_count, t.operator_request) == (Stage.IMPLEMENT, 2, None)
    assert [s[:2] for s in d.sessions.spawned] == [(N, "implement")]
    assert "implement_started" in d.notifier.sent


def test_an_approval_naming_an_unknown_track_is_bounced(c):
    wt = _at_the_gate(c)
    approve_plan(c, wt, SPEC_PATH)
    signal(wt, "done", stage="plan", track="nope")
    d = run(c)
    assert now(c).plan_slips == 1 and d.sessions.spawned == []


def test_an_approval_with_bad_tickets_retries(c):
    wt = _at_the_gate(c)
    (wt / ".agent" / "tickets" / "01-t1.md").write_text("# tiny\n")
    approve_plan(c, wt, SPEC_PATH)
    signal(wt, "done", stage="plan")
    d = run(c)
    assert now(c).plan_retries == 1 and len(d.sessions.resumed) == 1


def test_spec_done_hands_spec_and_track_to_plan(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="deep")
    d = run(c)
    t = now(c)
    assert (t.stage, t.spec_path, t.track) == (Stage.PLAN, "spec.md", "deep")
    assert [s[:2] for s in d.sessions.spawned] == [(N, "plan")]


def test_spec_done_with_an_unknown_track_is_bounced(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="nope")
    d = run(c)
    assert (now(c).stage, now(c).spec_retries) == (Stage.SPEC, 1)
    assert d.sessions.spawned == []


def test_spec_done_with_a_bad_spec_fails(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text("# tiny")
    signal(wt, "done", stage="spec", artifact="spec.md", track="standard")
    d = run(c)
    assert now(c).stage is Stage.FAILED and "artifact_failed" in d.notifier.sent


# --- crashes -------------------------------------------------------------------

def test_a_launch_that_raises_fails_at_the_stage_that_could_not_start(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="standard")
    other = make_task(c, issue=43, stage=Stage.REVIEW, updated_at=NOW, slot=1)
    signal(other, "done", stage="review",
           artifact="https://github.com/jesdi/portfolio_eval/pull/9")
    d = run(c, FakeSessions(alive={N, 43}, spawn_raises={N}))
    t = now(c)
    assert (t.stage, t.crashed_stage, t.spec_path) == (Stage.FAILED, "plan", "spec.md")
    assert (N, "task crashed mid-pass") in d.github.released
    assert load(c.state_dir, T, 43).stage is Stage.PR_OPEN


def test_any_other_error_in_a_turn_fails_the_task_and_the_pass_goes_on(c, monkeypatch):
    task(c)
    make_task(c, issue=43, stage=Stage.REVIEW, updated_at=NOW, slot=1)
    real = main.read_stage_signal

    def boom(worktree):
        if worktree.endswith(f"task-{N}"):
            raise RuntimeError("disk on fire")
        return real(worktree)

    monkeypatch.setattr(main, "read_stage_signal", boom)
    d = run(c, FakeSessions(alive={N, 43}))
    t = now(c)
    assert (t.stage, t.crashed_stage) == (Stage.FAILED, "implement")
    assert (N, "task crashed mid-pass") in d.github.released
    assert load(c.state_dir, T, 43).stage is Stage.REVIEW
