import json
import os
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
import telegram.inbound as inbound
from telegram.inbound import Reply
from dispatcher import stage_launch
from dispatcher.deps import Deps
from dispatcher import spec_publish
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (NO_SLOT, AnswersRequest, LoopCaps, PlanApprovalRequest, Stage, TaskState, has_waiting, load, load_all, mark_waiting, save, SessionRecord, write_session)
from tests.configfakes import with_project
from tests.dispatchfakes import (GOOD_TICKET, LOGIN_TAIL, PLAN_SUMMARY,
                                 SPEC_PATH, EveryRepoApp, FakeGitHub, FakeNotifier, FakeSessions,
                                 LiveUntilEnded, approve_plan, arm_gate, cfg, deps, gate_signal,
                                 make_task, patch_events, patch_teardown, patch_usage,
                                 patch_workspace, payload, pr_open_task, record_session,
                                 replace_capacity, valid_spec, write_tickets,
                                 SPEC_URL, _events, _plan_ready_task, feedback_pending, parked)
from tests.statuses import RUNNING, PR, human, answers, held, review, login, waking, failed
from dispatcher.status import Kind
from dispatcher.state import at_gate, operator_request


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


def test_digest(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.PLAN, gated=True, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    assert d.notifier.sent == ["daily_digest"]


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


def test_woken_task_resumes_before_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    c = replace_capacity(c, 1)
    wt = make_task(c, status=waking())
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]
    assert gh.claimed == []  # head-of-queue: resume consumed the only slot
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and t.park_msg_id == 0
    sig = json.loads((wt / ".agent" / "stage.json").read_text())
    assert sig["status"] == "working"  # rewritten before resume


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


def test_parked_task_does_not_block_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, status=human(), park_msg_id=55)
    gh = FakeGitHub([Candidate(99, "fresh", "u")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [99]


def test_pass_crash_files_issue_and_reraises(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions())

    def boom(cfg_, deps_, dry_run=False, config_path="targets.yaml"):
        raise RuntimeError("rank.py exploded")

    monkeypatch.setattr(main, "run_pass", boom)
    with pytest.raises(RuntimeError):
        main.guarded_pass(c, d, "targets.yaml")

    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops"
    assert "pass-crash" in title and "(dispatcher)" in title
    assert "rank.py exploded" in body
    assert "agent-ops-dispatcher --config targets.yaml" in body
    assert "task_failed" in d.notifier.sent


def test_pass_crash_recurring_dedupes_to_one_issue(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions())
    monkeypatch.setattr(main, "run_pass",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("rank.py exploded")))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            main.guarded_pass(c, d, "targets.yaml")
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1


def test_broken_worktree_fails_that_task_and_pass_survives(tmp_path, monkeypatch):
    """A woken task whose worktree vanished must not take the pass with it.

    The box wedged this way for hours: task-194's checkout was gone, so
    the launch raised inside sessions.resume, guarded_pass re-raised, and
    _drive_wakes never reached the tasks behind it — no claims, no spawns,
    nothing, every pass, until a human looked."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    write_session(c.state_dir, "portfolio_eval", 43, SessionRecord("other-session", "implement"))
    make_task(c, issue=42, status=waking(), slot=0,
              updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, status=waking(), slot=1,
              updated_at="2026-07-22T00:00:00+00:00")
    gh = FakeGitHub()
    sess = FakeSessions(resume_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    # The task behind the broken one still got its turn.
    assert [r[0] for r in sess.resumed] == [43]
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.RUNNING
    # The broken one is failed by the crash and off the board's live columns.
    broken = load(c.state_dir, "portfolio_eval", 42)
    assert broken.status == failed("task-crash")
    assert (42, "task crashed mid-pass") in gh.released
    # Filed on the infra repo: a vanished worktree is a box-side problem
    # no session container can fix.
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops"
    assert "task-crash" in title and "#42" in title
    assert "No such file or directory" in body
    assert "task_failed" in d.notifier.sent


def test_broken_worktree_on_spawn_fails_that_task_and_pass_survives(
        tmp_path, monkeypatch):
    """Same isolation on the spawn path: _drive_task advancing a stage into
    a vanished worktree fails the task, not the pass."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Gate stage with a dead session: _drive_task respawns it in place.
    make_task(c, issue=42, stage=Stage.PLAN, gated=True, slot=0)
    gh = FakeGitHub()
    sess = FakeSessions(spawn_raises=[42])
    d = deps(gh, sess)

    main.run_pass(c, d)  # must not raise

    assert sess.spawned == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert "task-crash" in gh.created_issues[0][1]


def test_repository_specific_policy_is_used_at_spawn(tmp_path, monkeypatch):
    """Finding D: a per-repository `models:` override was only ever verified in
    isolation against `policy_for` — never end-to-end through `_spawn_stage`.
    The global POLICY's untracked/standard track resolves to Opus; the
    repository's OWN policy must win instead and resolve to its own model."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    repository_policy = parse_policy({
        "triage": ["claude-sonnet-5"],
        "untracked": "standard",
        "tracks": {"standard": {
            "when": "Everything.",
            "spec": ["claude-fable-5-1"], "plan": ["claude-fable-5-1"],
            "implement": ["claude-fable-5-1"], "review": ["claude-fable-5-1"]}},
    })
    c = dc_replace(c, repositories=[with_project(c.repositories[0], models=repository_policy)])
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=1, labels=("auto",))])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-fable-5-1")]


from dispatcher import eventlog


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


from dispatcher import intents as intents_mod


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



def test_migrate_tmux_flag_migrates_and_its_callback_really_wakes(
        tmp_path, monkeypatch):
    """provision/update.sh runs this under `set -e`, so a wiring slip here
    fails the whole convergence pass on every firing while any tmux session
    exists. Assert the flag reaches tmux_migration.migrate with the state
    dir and a wake callable that IS `_wake` — park=PARK_WAKE plus a queued
    message — and that the branch takes no pass_lock (update.sh already
    holds that file; flock is per open-file-description, so re-taking it
    would block forever)."""
    import sys as _sys
    from dispatcher import messages

    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    monkeypatch.setattr(main, "load_config", lambda path: c)

    def _no_lock(state_dir):
        raise AssertionError("--migrate-tmux must not take pass_lock")

    monkeypatch.setattr(main, "pass_lock", _no_lock)

    seen = {}

    def fake_migrate(state_dir, wake):
        seen["state_dir"], seen["wake"] = state_dir, wake
        return ["killed tmux session task-portfolio_eval-42"]

    monkeypatch.setattr(main.tmux_migration, "migrate", fake_migrate)
    monkeypatch.setattr(_sys, "argv",
                        ["agent-ops-dispatcher", "--config",
                         str(tmp_path / "targets.yaml"), "--migrate-tmux"])

    main.main()

    assert seen["state_dir"] == c.state_dir
    assert callable(seen["wake"])
    # The callback is the real _wake: driving it must park the task for a
    # resume and queue the text the resumed claude will be handed.
    seen["wake"](load(c.state_dir, "portfolio_eval", 42), "moved to herdr")
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "moved to herdr"]


# ---------------------------------------------------------------------------
# Task 6: dispatcher wiring — stages, tickets, rounds, parks, notifications
# ---------------------------------------------------------------------------


def events(c, name):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == name]


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
    assert [e["stage"] for e in events(c, "stage-started")] == ["implement"]


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
    assert "pr_opened" in d2.notifier.sent and len(events(c, "pr-opened")) == 1


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
    assert events(c, "round")[-1]["detail"] == "gate round 1/2"
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
    parked_events = events(c, "parked")
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
    assert [e["transition"]["event"] for e in events(c, "transition")] == [
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
    wt = make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
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
