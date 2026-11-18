import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
import telegram.inbound as inbound
from telegram.inbound import Reply
from dispatcher import stage_launch
from dispatcher.state import (NO_SLOT, AnswersRequest, LoopCaps, Stage, TaskState, has_waiting, load, mark_waiting, save, SessionRecord, write_session)
from tests.dispatchfakes import (FakeGitHub, FakeNotifier, FakeSessions,
                                 approve_plan, cfg, deps, gate_signal,
                                 make_task, patch_events, patch_usage,
                                 patch_workspace, payload, pr_open_task, replace_capacity, valid_spec, write_tickets, _events)
from tests.statuses import RUNNING, PR, human, waking
from dispatcher.status import Kind
from dispatcher.state import operator_request
from dispatcher import eventlog
from dispatcher import intents as intents_mod


def test_spec_signal_without_a_track_is_bounced_once_then_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "spec"))
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard",
                   picks={"spec": "anthropic/claude-opus-5"})
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md"}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    (resume,) = sess.resumed
    assert "must be one of" in resume[1] and "'deep'" in resume[1]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.SPEC and t.spec_retries == 1
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"
    # second time: park for the operator
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md"}))
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and "must be one of" in t.status.note
    assert t.asked is False   # a bounce park is not a questionnaire


def test_spec_awaiting_review_is_bounced_to_done_and_never_waits_for_a_review(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), spec_review_grace_minutes=0)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard",
                   picks={"spec": "anthropic/claude-opus-5"})
    valid_spec(wt)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    gate_signal(wt)
    main.run_pass(c, d)
    (resume,) = sess.resumed
    assert "no review gate" in resume[1] and '"done"' in resume[1]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.SPEC and t.status.kind is Kind.RUNNING and operator_request(t) is None
    # second time: a question for the operator, not a review
    gate_signal(wt)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.SPEC and t.status.kind is Kind.PARKED_HUMAN
    assert d.notifier.sent == ["parked_question"]
    assert t.asked is False   # a bounce park is not a questionnaire


def test_asked_is_kept_when_the_spec_session_parks_again_without_a_questionnaire(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard",
                   picks={"spec": "anthropic/claude-opus-5"})
    (wt / ".agent" / "questionnaire.md").write_text("# Q\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers",
         "artifact": ".agent/questionnaire.md"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 42).asked is True
    # The operator answers; the resumed session then reports it is blocked.
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                         {"text": "a, b, c"}, actor="op", epoch_ms=1)
    main.run_pass(c, deps(sess=sess))
    assert len(sess.resumed) == 1
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "blocked", "note": "no access to the repo"}))
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and t.status.note == "no access to the repo"
    assert t.asked is True


def test_a_plan_session_that_parks_for_answers_is_recorded_as_asking(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, issue=42, stage=Stage.PLAN, track="standard")
    (wt / ".agent" / "questions.md").write_text("# Q\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-answers",
         "artifact": ".agent/questions.md"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_ANSWERS and t.asked is True


def test_spec_signal_with_a_misspelled_track_is_bounced_not_mis_parked(tmp_path, monkeypatch):
    """A signal naming an UNKNOWN track (not merely a missing one) must not be
    adopted onto the task before the "track configured" guard runs — doing so
    would park the task immediately with the bad value baked into state,
    instead of letting next_actions' bounce-then-park ladder run (resume once
    with the reason, park only on the second failure — same as the missing-
    track case above)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "spec"))
    wt = make_task(c, issue=42, stage=Stage.SPEC, track="standard",
                   picks={"spec": "anthropic/claude-opus-5"})
    valid_spec(wt)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "artifact": "spec.md",
         "track": "tivial"}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    (resume,) = sess.resumed
    assert "must be one of" in resume[1] and "'tivial'" in resume[1]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.SPEC and t.spec_retries == 1
    assert t.track == "standard", "the unknown track must not be adopted onto the task"
    assert t.status.kind is Kind.RUNNING
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"


def test_spec_done_advances_to_plan(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.repositories[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    spec = wt / "spec.md"
    spec.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                    + "\n\n## Decisions\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md", "track": "standard"}))
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.SPEC, slot=0,
                                worktree=str(wt), branch="agent/task-42",
                                title="t", track="standard",
                                updated_at="2026-07-14T00:00:00+00:00"))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert [s[:3] for s in sess.spawned] == [(42, "plan", "anthropic/claude-opus-5")]
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PLAN


def test_plan_format_failure_retries_in_session_then_fails(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    wt = Path(c.repositories[0].worktrees_path) / "task-42"
    (wt / ".agent" / "tickets").mkdir(parents=True)
    # Malformed ticket set: too small and missing required patterns
    (wt / ".agent" / "tickets" / "01-bad.md").write_text("# tiny\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "note": "",
         "artifact": ".agent/review.html"}))
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.PLAN, slot=0, worktree=str(wt),
                                branch="agent/task-42", title="t",
                                track="standard",
                                updated_at="2026-07-14T00:00:00+00:00"))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    # First pass: resumed in place (zombie ended first), not failed.
    assert 42 in sess.ended
    assert len(sess.resumed) == 1 and sess.resumed[0][0] == 42
    assert "tickets" in sess.resumed[0][1]
    assert "plan_retry" in d.notifier.sent
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.PLAN and t.plan_retries == 1
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"

    # Session reports ready again but the tickets are still malformed → retry exhausted.
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "note": "",
         "artifact": ".agent/review.html"}))
    d2 = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d2)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert "artifact_failed" in d2.notifier.sent
    # the operator was never asked to review a plan with a bad ticket set
    assert "awaiting_plan_review" not in d.notifier.sent + d2.notifier.sent


def test_stage_advance_ends_previous_session_before_spawn(tmp_path, monkeypatch):
    """An interactive claude cannot exit itself, so on stage advance the
    previous session is usually still alive. Spawning without ending it
    first makes _launch type the next stage's podman command INTO the old
    claude's input box (and the container name would collide anyway) —
    task #192's plan stage was 'spawned' straight into the zombie spec
    session. End first, then spawn."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.repositories[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    spec = wt / "spec.md"
    spec.write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                    + "\n\n## Decisions\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md", "track": "standard"}))
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.SPEC, slot=0,
                                worktree=str(wt), branch="agent/task-42",
                                title="t", track="standard",
                                updated_at="2026-07-14T00:00:00+00:00"))

    class OrderedSessions(FakeSessions):
        def __init__(self, alive=()):
            super().__init__(alive)
            self.ops = []

        def end(self, repository, issue):
            self.ops.append(("end", issue))
            super().end(repository, issue)

        def spawn_stage(self, repository, issue, worktree, prompt, stage_name, model, effort="",
                    second=None, *, gh_config):
            self.ops.append(("spawn", stage_name))
            super().spawn_stage(repository, issue, worktree, prompt, stage_name, model, effort, gh_config=gh_config)

    sess = OrderedSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert sess.ops == [("end", 42), ("spawn", "plan")]


def test_park_for_ci_appends_parked_event_with_run_id_detail(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "awaiting-ci", "run_id": 4242}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    parked = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]
    assert parked[0]["detail"] == "awaiting CI run 4242"


def test_resume_appends_resumed_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, status=waking())
    main.run_pass(c, deps(sess=FakeSessions()))
    resumed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "resumed"]
    assert len(resumed) == 1
    assert resumed[0]["issue"] == 42 and resumed[0]["stage"] == "implement"
    assert resumed[0]["model"] == "anthropic/claude-opus-5"


def test_review_done_appends_pr_opened_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "done", "note": "PR #9"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN
    opened = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "pr-opened"]
    assert len(opened) == 1 and opened[0]["issue"] == 42


# ---------------------------------------------------------------------------
# Task 6: dispatcher wiring — stages, tickets, rounds, parks, notifications
# ---------------------------------------------------------------------------


def test_approved_plan_starts_one_implement_session_over_the_tickets(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.PLAN, gated=True, spec_path="docs/specs/x-design.md")
    write_tickets(wt, 3)
    approve_plan(c, wt, "docs/specs/x-design.md")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "note": "3 tickets", "artifact": ".agent/tickets"}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.ticket_count) == (Stage.IMPLEMENT, 3)
    issue, stage_name, model, prompt, effort = sess.spawned[-1]
    assert stage_name == "implement"
    assert "`.agent/tickets`" in prompt and "docs/specs/x-design.md" in prompt
    assert ".agent/tickets/0" not in prompt      # no ticket of its own
    assert "implement_started" in d.notifier.sent
    assert [e["stage"] for e in _events(c, "stage-started")] == ["implement"]


def test_spec_approval_records_the_spec_path_for_later_stages(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text("# t — design\n\n## Problem\n\n" + "x " * 400
                                + "\n\n## Decisions\n\n" + "y " * 400)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "done", "note": "", "artifact": "spec.md", "track": "standard"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).spec_path == "spec.md"


def test_implement_done_spawns_review_and_review_done_opens_pr(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT, ticket_count=2, gate_rounds=2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "done", "note": ""}))
    sess = FakeSessions(alive={42}); d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.gate_rounds) == (Stage.REVIEW, 0)
    assert 42 in sess.ended
    assert sess.spawned[-1][1] == "review" and "review_started" in d.notifier.sent
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "done", "note": "https://github.com/o/r/pull/9",
         "artifact": "https://github.com/o/r/pull/9"}))
    d2 = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d2)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.pr_number) == (Stage.PR_OPEN, 9)
    assert "pr_opened" in d2.notifier.sent and len(_events(c, "pr-opened")) == 1


def test_pr_opens_and_parks_on_github_ending_the_review_session_once(tmp_path, monkeypatch):
    """Scenario "PR opens and parks on GitHub": the pr-open transition ends the
    review session; neither the next pass nor the feedback round ends it again."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.REVIEW, slot=2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "done", "note": "https://github.com/o/r/pull/9",
         "artifact": "https://github.com/o/r/pull/9"}))
    sess, gh = FakeSessions(alive={42}), FakeGitHub()
    d = deps(gh, sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status, t.slot) == (Stage.PR_OPEN, PR, NO_SLOT)
    assert sess.ended == [42] and "pr_opened" in d.notifier.sent
    main.run_pass(c, d)
    assert sess.ended == [42]
    gh.pr_payloads[9] = payload(comments=[{"createdAt": "2099-01-01T00:00:00Z",
                                           "author": {"login": "rev"}, "body": "fix"}])
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.ADDRESS_REVIEW
    assert sess.ended == [42, 42]  # the pr-open park, then the launch's own end


def test_blocked_implement_session_parks_in_its_stage(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT, ticket_count=5)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "need API key"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status, t.ticket_count) == (
        Stage.IMPLEMENT, human("decision", "need API key"), 5)


def test_awaiting_answers_parks_and_records_the_artifact(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC)
    (wt / ".agent" / "questionnaire.md").write_text("# Q\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "5 questions",
         "artifact": ".agent/questionnaire.md"}))
    notif = FakeNotifier(); sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_ANSWERS
    assert operator_request(t) == AnswersRequest(path=".agent/questionnaire.md")
    assert 42 in sess.ended and "parked_question" in notif.sent


def test_awaiting_answers_sets_answers_operator_request(tmp_path, monkeypatch):
    """Slice 5: awaiting-answers with valid artifact → operator_request set, spec_path unchanged."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC, spec_path="docs/specs/design.md")
    (wt / ".agent" / "questionnaire.md").write_text("# Q\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "answer me",
         "artifact": ".agent/questionnaire.md"}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42}), notifier=FakeNotifier()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(t) == AnswersRequest(path=".agent/questionnaire.md")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be unchanged"
    assert t.status.kind is Kind.PARKED_ANSWERS


@pytest.mark.parametrize("artifact_value,label", [
    ("", "empty-artifact"),
    ("../../etc/passwd", "escaping-path"),
])
def test_malformed_awaiting_answers_parks_with_diagnostic_no_request(
        tmp_path, monkeypatch, artifact_value, label):
    """Slice 6: awaiting-answers with unusable artifact → diagnostic park, no
    request. Escaping path must not crash (Minor A)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.SPEC, spec_path="docs/specs/design.md")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "spec", "status": "awaiting-answers", "note": "please answer",
         "artifact": artifact_value}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42}), notifier=FakeNotifier()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN, "must be parked"
    assert operator_request(t) is None, (
        f"malformed answers must set no request, not borrow prior {operator_request(t)!r}")
    assert "malformed" in (t.status.note or "").lower() or "unusable" in (t.status.note or "").lower(), (
        f"park_note must convey the malformed-answers reason, got: {t.status.note!r}")


def test_gate_round_is_counted_pinged_and_parked_past_the_cap(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.REVIEW)
    sig = wt / ".agent" / "stage.json"
    sig.write_text(json.dumps({"stage": "review", "status": "working", "loop": "gate", "round": 1}))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    assert load(c.state_dir, "portfolio_eval", 42).gate_rounds == 1
    assert _events(c, "round")[-1]["detail"] == "gate round 1/2"
    sig.write_text(json.dumps({"stage": "review", "status": "working", "loop": "gate", "round": 2}))
    d = deps(sess=FakeSessions(alive={42})); main.run_pass(c, d)
    assert "last_round" in d.notifier.sent
    sig.write_text(json.dumps({"stage": "review", "status": "working", "loop": "gate", "round": 3}))
    d = deps(sess=FakeSessions(alive={42})); main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and t.gate_rounds == 3 and "parked_question" in d.notifier.sent


def test_session_exhaustion_ends_session_and_clears_waiting(tmp_path, monkeypatch):
    """Session path: exhausted cap must park via _park_for_input, not _park_exhausted.
    That means: session.end() is called, waiting marker is cleared, parked event
    detail is plain note (no 'loop exhausted: ' prefix)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.REVIEW, gate_rounds=2)
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "review", "status": "working", "loop": "gate", "round": 3}))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN, "task must be parked"
    assert 42 in sess.ended, "session container must be ended on exhaustion"
    assert not has_waiting(c.state_dir, "portfolio_eval", 42), "waiting marker must be cleared"
    parked_events = _events(c, "parked")
    assert parked_events, "parked event must be written"
    detail = parked_events[-1]["detail"]
    assert detail == "gate loop exceeded its cap of 2 rounds", (
        f"parked detail must be plain note (no 'loop exhausted: ' prefix); got: {detail!r}")


def test_operator_wake_resets_the_round_budget(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=human(), park_msg_id=77, slot=NO_SLOT,
              gate_rounds=3, e2e_rounds=1)
    d = deps(sess=FakeSessions())
    monkeypatch.setattr(inbound, "fetch_events", lambda state_dir: [
        Reply(reply_to_msg_id=77, text="use the mock, skip the real gateway")])
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.gate_rounds, t.e2e_rounds) == (0, 0)
    assert t.status.kind is Kind.RUNNING and d.sessions.resumed  # woken and resumed in the same pass


def test_operator_wake_on_a_parked_pr_open_task_is_a_fresh_address_review_round(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(human("operator", "ci loop exceeded")))
    stage_launch._queue_message(c, "portfolio_eval", 42, "the flaky test is known — skip it", "operator")
    sess = FakeSessions(); main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind) == (Stage.ADDRESS_REVIEW, Kind.RUNNING)
    assert "Reason for this round: operator" in sess.spawned[-1][3]
    assert sess.spawned[-1][1] == "address-review" and "skip it" in sess.spawned[-1][3]


def test_pr_feedback_wakes_address_review(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(replace_capacity(cfg(tmp_path), 0), loop_caps=LoopCaps(ci=3))
    pr_open_task(c, status=PR)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status, t.ci_rounds) == (
        Stage.PR_OPEN, waking(PR, "attention(feedback)"), 0)
    assert [e["transition"]["event"] for e in _events(c, "transition")] == [
        "pr-feedback", "resume-admitted"]   # the round waits for capacity
    note = next(ctx["note"] for tmpl, ctx in notif.calls if tmpl == "pr_feedback")
    assert "2026-09-07T09:00:00Z" in note


def test_an_admitted_feedback_wake_runs_address_review_with_the_feedback(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=3))
    pr_open_task(c, status=PR)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status, t.ci_rounds) == (Stage.ADDRESS_REVIEW, RUNNING, 0)
    [(_, stage, _, prompt, _)] = sess.spawned
    assert stage == "address-review" and "Reason for this round: feedback" in prompt


def test_telegram_reply_operator_wake_clears_all_four_counters(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55,
              review_rounds=1, gate_rounds=2, e2e_rounds=1, ci_rounds=3)
    make_task(c, issue=43)  # holds the only slot → 42 stays waking
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55, text="try again")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0)
