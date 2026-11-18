import json
from datetime import datetime, timezone
from pathlib import Path


import dispatcher.main as main
from dispatcher import stage_launch
from dispatcher.state import (NO_SLOT, AnswersRequest, PlanApprovalRequest, Stage, has_waiting, load, mark_waiting, SessionRecord, write_session)
from tests.dispatchfakes import (LOGIN_TAIL, FakeGitHub, FakeNotifier, FakeSessions,
                                 cfg, deps, gate_signal,
                                 make_task, patch_teardown, patch_usage,
                                 patch_workspace, payload, pr_open_task, replace_capacity, write_tickets,
                                 _events, feedback_pending)
from tests.statuses import RUNNING, PR, human, answers, held, login, waking, failed
from dispatcher.status import Kind
from dispatcher.state import at_gate, operator_request
from dispatcher import intents as intents_mod


def test_blocked_session_parks_and_frees_capacity(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need a decision"}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and t.park_msg_id == 77
    assert t.stage is Stage.IMPLEMENT  # stage preserved while parked
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


def test_waiting_marker_pokes_stopped_session(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    sess = FakeSessions(alive={42}, idle={42: 30.0})
    sess.agent_state = lambda repository, issue: ("done", 5)   # read again before typing
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and len(sess.sent_text) == 1 and t.pokes == 1
    assert has_waiting(c.state_dir, "portfolio_eval", 42)  # the poke leaves the marker


def test_awaiting_ci_parks_silently_with_run_id(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 4242}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_CI and t.status.run_id == 4242
    assert sess.ended == [42]
    assert "parked_question" not in d.notifier.sent


def test_hold_for_attach_resumes_without_reply_injection(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, status=waking(), hold_for_attach=True)
    sess = FakeSessions()
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert len(sess.resumed) == 1 and "attaching" in sess.resumed[0][1]
    assert "resumed_for_attach" in d.notifier.sent


def test_a_stale_attached_marker_no_longer_holds_anything(tmp_path, monkeypatch):
    """Regression test: now that attached markers are gone, a stale file
    on disk has no power. Tasks resume and drive normally."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=waking())
    # Simulate a stale marker file left from old runs
    (Path(c.state_dir) / "attached-portfolio_eval-42").touch()
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    # The stale marker is ignored; the task resumes normally
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING


def test_resume_clears_park_note(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, status=waking(human("operator", "stale question")))
    main.run_pass(c, deps(sess=FakeSessions()))
    assert load(c.state_dir, "portfolio_eval", 42).status.note == ""


# ---------------------------------------------------------------------------
# PR lifecycle tests (Task 8)
# ---------------------------------------------------------------------------


def test_review_done_captures_pr_number(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "review", "status": "done",
        "note": "https://github.com/jesdi/portfolio_eval/pull/77",
        "artifact": "https://github.com/jesdi/portfolio_eval/pull/77"}))
    main.run_pass(c, deps(FakeGitHub()))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN and got.pr_number == 77


def test_pending_feedback_spawns_address_review_when_capacity_frees(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()  # quiet now; pending flag drives the spawn
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.ADDRESS_REVIEW
    assert feedback_pending(got) is False
    assert got.feedback_cursor != ""      # cursor = spawn time
    assert got.slot != NO_SLOT
    assert [s for s in sess.spawned if s[1] == "address-review"]
    assert sess.ended == [42]  # any pane still there is ended before the launch


def test_pending_feedback_not_spawned_over_capacity(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=1, stage=Stage.IMPLEMENT, slot=0)  # eats the capacity
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions(alive=(1,))
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN and feedback_pending(got) is True


def test_pending_feedback_not_spawned_when_budget_low(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.99)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN
    assert sess.spawned == []


# ---------------------------------------------------------------------------
# PR lifecycle — final-review fixes
# ---------------------------------------------------------------------------


def test_address_review_reuses_the_feedback_pick(tmp_path, monkeypatch):
    """A pr-open task's feedback round reuses the task's sticky feedback
    pick — not the implement pick, and not a fresh policy resolution (the
    deep track's implement list would give opus@medium)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(PR, "attention(feedback)"), track="deep",
                 picks={"feedback": "anthropic/claude-sonnet-5@low",
                        "implement": "anthropic/claude-opus-5@medium"},
                 implement_providers=["anthropic"])
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] + (s[4],) for s in sess.spawned] == [
        (42, "address-review", "anthropic/claude-sonnet-5", "low")]


def test_park_for_ci_frees_the_slot(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, slot=2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 4242}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_CI and t.slot == NO_SLOT


def test_login_hold_keeps_its_slot(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1, status=login())
    assert load(c.state_dir, "portfolio_eval", 42).slot == 1


def test_the_save_frees_the_slot_of_a_status_that_does_not_occupy(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=0, status=human())
    make_task(c, issue=43, slot=1, status=waking())
    make_task(c, issue=44, slot=2, status=RUNNING)          # live: untouched
    assert load(c.state_dir, "portfolio_eval", 42).slot == NO_SLOT
    assert load(c.state_dir, "portfolio_eval", 43).slot == NO_SLOT
    assert load(c.state_dir, "portfolio_eval", 44).slot == 2


# Slice 11: successful resume clears operator_request, preserves spec_path
# ---------------------------------------------------------------------------

def test_successful_resume_clears_operator_request_preserves_spec_path(
        tmp_path, monkeypatch):
    """Slice 11: normal-path resume clears operator_request; spec_path unchanged; GET /request → null."""
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS as WEB_HEADERS
    from web.app import create_app
    from web.sources import Sources
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    req = AnswersRequest(path=".agent/questionnaire.md")
    make_task(c, status=waking(answers(req)), spec_path="docs/spec.md")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) is None, f"expected cleared, got {operator_request(t)}"
    assert t.spec_path == "docs/spec.md", "spec_path must be preserved"
    assert t.status.kind is Kind.RUNNING
    # GET /request must return 200 null after the request is cleared
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request", headers=WEB_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() is None


def test_successful_pr_open_resume_clears_operator_request(tmp_path, monkeypatch):
    """Slice 11: pr-open path resume (_spawn_stage ADDRESS_REVIEW) also clears operator_request."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    req = PlanApprovalRequest(".agent/review.html")
    make_task(c, stage=Stage.PR_OPEN, status=waking(answers(req)), slot=NO_SLOT, spec_path="docs/spec.md")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) is None, f"expected cleared on pr-open resume, got {operator_request(t)}"
    assert t.spec_path == "docs/spec.md", "spec_path must be preserved"
    assert t.status.kind is Kind.RUNNING


# Slice 12 tests

def test_resumed_gate_task_rearms_approval(tmp_path, monkeypatch):
    """Slice 12: task at gate with operator_request=None (cleared by resume);
    PLAN session re-signals awaiting-review → operator_request set to the
    signal's summary, spec_path and stage unchanged. GET /request →
    plan-approval body. (The new round's publish, notification and clock are
    in test_ready_again_after_a_resume_is_a_new_review_round.)"""
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS as WEB_HEADERS
    from web.app import create_app
    from web.sources import Sources
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    # Fresh updated_at so grace is NOT elapsed (15-minute window).
    fresh_ts = datetime.now(timezone.utc).isoformat()
    wt = make_task(c, stage=Stage.PLAN, gated=True,
                   spec_path="specs/x/spec.md",
                   updated_at=fresh_ts)
    write_tickets(wt, 1)
    gate_signal(wt)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t), "stage must stay unchanged"
    assert operator_request(t) == PlanApprovalRequest(".agent/review.html"), "must re-arm approval"
    assert t.spec_path == "specs/x/spec.md", "spec_path must NOT become the summary path"
    # GET /request must return plan-approval once re-armed
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request", headers=WEB_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "plan-approval"


# ---------------------------------------------------------------------------
# Slice 13: ordinary input park + loop-exhaustion park ask nothing of a page
# ---------------------------------------------------------------------------

def test_ordinary_park_at_the_gate_drops_the_request(tmp_path):
    """Slice 13: _park_for_input of a plan held at the gate parks it for the
    operator's question, with no request. spec_path and counters must be
    preserved."""
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
              spec_path="docs/specs/design.md",
              review_rounds=1, gate_rounds=2, e2e_rounds=0, ci_rounds=1)
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._park_for_input(c, deps(notifier=FakeNotifier()), c.repositories[0],
                         task, human("decision", "blocked by test"))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) is None, (
        f"ordinary park must ask no page, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"
    assert t.status == human("decision", "blocked by test")
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 2, 0, 1), (
        "loop counters must be unchanged")


# ---------------------------------------------------------------------------
# Slice 14: stage advance clears operator_request, preserves spec_path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Slice 15: terminal and superseding transitions clear operator_request
# ---------------------------------------------------------------------------


def test_done_transition_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15c: merged PR → DONE clears operator_request; spec_path + done_at preserved."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, spec_path="docs/specs/design.md")
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-09-08T10:00:00Z")
    main.run_pass(c, deps(gh))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.DONE, f"expected DONE, got {t.stage}"
    assert operator_request(t) is None, (
        f"DONE must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"
    assert t.done_at, "done_at must be set"
    # GET /request → 200 null
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS as WEB_HEADERS
    from web.app import create_app
    from web.sources import Sources
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request",
                              headers=WEB_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() is None


def test_ci_park_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15d: a CI park clears operator_request; spec_path + ci_run_id preserved."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, spec_path="docs/specs/design.md",
                   review_rounds=1, gate_rounds=0, e2e_rounds=0, ci_rounds=2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 9999}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_CI and t.status.run_id == 9999
    assert operator_request(t) is None, (
        f"CI park must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 0, 0, 2), (
        "loop counters must be unchanged")
    # GET /request → 200 null
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS as WEB_HEADERS
    from web.app import create_app
    from web.sources import Sources
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request",
                              headers=WEB_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() is None


def test_login_park_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15e: login stall → PARK_LOGIN clears operator_request; spec_path preserved."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, spec_path="docs/specs/design.md")
    sess = FakeSessions(alive=[42], idle={42: 999999.0}, tail=LOGIN_TAIL)
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.HELD_LOGIN
    assert operator_request(t) is None, (
        f"login park must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"
    # GET /request → 200 null
    from fastapi.testclient import TestClient
    from tests.webfakes import HEADERS as WEB_HEADERS
    from web.app import create_app
    from web.sources import Sources
    sources = Sources(c.state_dir, sessions=None, github=None)
    with TestClient(create_app(c, sources)) as client:
        response = client.get("/api/task/portfolio_eval/42/request",
                              headers=WEB_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() is None


# ---------------------------------------------------------------------------
# Slice 15 fix: dedicated tests for the three other FAILED save sites
# ---------------------------------------------------------------------------


# --- Resume after a crash (#363): respawn the stage it died in -------------

def _crash_implement(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT, ticket_count=2)
    (wt / ".agent" / "tickets").mkdir()
    for name in ("01-seams.md", "02-logout.md"):
        (wt / ".agent" / "tickets" / name).write_text(f"# {name}\n")
    main.run_pass(c, deps(sess=FakeSessions(alive=set())))
    return c


def test_a_crash_remembers_the_stage_it_failed_in(tmp_path, monkeypatch):
    c = _crash_implement(tmp_path, monkeypatch)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.IMPLEMENT, failed("session-crash"))


def test_resume_respawns_the_crashed_stage_fresh(tmp_path, monkeypatch):
    c = _crash_implement(tmp_path, monkeypatch)
    stage_launch._queue_message(c, "portfolio_eval", 42, "Now what?", "op")
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    gh, sess = FakeGitHub(), FakeSessions()
    main.run_pass(c, deps(gh, sess))

    assert sess.resumed == [], "no --continue: the transcript may be another stage's"
    (issue, stage, _model, prompt, _effort) = sess.spawned[0]
    assert (issue, stage) == (42, "implement")
    assert "implement-spec/SKILL.md" in prompt and "02-logout.md" not in prompt
    assert "Now what?" in prompt and "The operator resumed this task" in prompt
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.ticket_count) == (Stage.IMPLEMENT, Kind.RUNNING, 2)
    assert t.slot != NO_SLOT
    assert (42, "In progress") in gh.statused
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_resume_of_a_migrated_failure_with_no_known_stage_is_rejected(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, status=failed("unknown"), slot=NO_SLOT,
              updated_at=datetime.now(timezone.utc).isoformat())  # inside retention
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.spawned == [] and sess.resumed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    [e] = _events(c, "transition-rejected")
    assert e["detail"] == "re-queue the issue instead"


def test_a_launch_that_raises_on_resume_fails_resumable_again(tmp_path, monkeypatch):
    c = _crash_implement(tmp_path, monkeypatch)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(spawn_raises={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.IMPLEMENT, failed("task-crash"))
