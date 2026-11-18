import json
import os
import socket
import subprocess
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import stage_launch
from dispatcher.deps import Deps, _now
from dispatcher import failures, spec_publish, state
from dispatcher.board import BoardError
from dispatcher.github import Candidate
from dispatcher.pr_poll import CIStatus
from dispatcher.models import parse_policy
from dispatcher.state import (NO_SLOT, AnswersRequest, LoopCaps, PlanApprovalRequest, Stage, TaskState, delete, has_waiting, load, load_all, mark_waiting, save, SessionRecord, write_session)
from tests.usagefakes import session_usage
from tests.configfakes import with_project
from tests.dispatchfakes import (ADMIT_ALL, GOOD_TICKET, LOGIN_TAIL, PLAN_SUMMARY,
                                 SPEC_PATH, EveryRepoApp, FakeGitHub, FakeNotifier, FakeSessions,
                                 LiveUntilEnded, approve_plan, arm_gate, cfg, deps, gate_signal,
                                 make_task, patch_events, patch_teardown, patch_usage,
                                 patch_workspace, payload, pr_open_task, record_session,
                                 replace_capacity, valid_spec, write_tickets,
                                 SPEC_URL, _events, _plan_ready_task)
from tests.statuses import RUNNING, PR, CANCELED, DONE, human, answers, held, review, login, access, ci, waking, failed
from dispatcher.status import Cause, HumanReason, Kind
from dispatcher.state import at_gate, operator_request


def row(number, title="t", status="Ready", labels=("auto",), blocked=False,
        score=1.0, boost=0):
    return {"number": number, "title": title, "url": f"u{number}",
            "status": status, "labels": list(labels), "blocked": blocked,
            "score": score, "boost": boost}


def parked(task):
    """No session runs for the task: it waits for the operator, a wake or an event."""
    return task.status.kind not in (Kind.RUNNING, Kind.HELD_PLAN_APPROVAL)


def feedback_pending(task):
    """Woken by its PR's attention, the address-review round not launched yet."""
    return task.status.kind is Kind.WAKING and task.status.cause.value.startswith("attention(")


def test_new_candidate_claimed_and_spec_spawned(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh, sess))

    assert gh.claimed == [42]
    assert [s[:3] for s in sess.spawned] == [(42, "spec", "anthropic/claude-opus-5")]
    ts = load(c.state_dir, "portfolio_eval", 42)
    assert ts.stage is Stage.SPEC and ts.slot == 0 and ts.title == "Add widget"
    sig = json.loads(
        (Path(c.repositories[0].worktrees_path) / "task-42" / ".agent" / "stage.json")
        .read_text())
    assert sig == {"stage": "spec", "status": "working",
                   "model": "anthropic/claude-opus-5", "effort": ""}


def test_no_claim_while_a_task_file_cannot_be_read(tmp_path, monkeypatch):
    """A task in a shape the loader refuses may hold the very issue a claim
    would take again, and a unit and a slot no count can see: the claim
    round waits until the file is readable (migrated or repaired)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(c.state_dir) / "task-portfolio_eval-42.json").write_text(json.dumps(
        {"issue": 42, "repository": "portfolio_eval", "stage": "awaiting-plan-review",
         "slot": 0, "worktree": "/wt", "branch": "agent/task-42", "title": "t",
         "updated_at": "2026-07-21T00:00:00+00:00"}))
    gh = FakeGitHub([Candidate(42, "Add widget", "u42"), Candidate(43, "Other", "u43")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []


def test_claim_snapshots_effort_and_labels(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    gh = FakeGitHub([Candidate(42, "T", "u42", effort=3,
                               labels=("auto", "frontend"))])
    c = cfg(tmp_path)
    main.run_pass(c, deps(gh))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.effort == 3
    assert t.labels == ("auto", "frontend")


def test_capacity_blocks_new_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    for i, issue in enumerate([1, 2, 3]):
        wt = Path(c.repositories[0].worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True)
        save(c.state_dir, TaskState(issue=issue, repository="portfolio_eval",
                                    stage=Stage.SPEC, slot=i, worktree=str(wt),
                                    branch=f"agent/task-{issue}", title="t",
                                    updated_at="2026-07-14T00:00:00+00:00",
                                    track="standard"))
    gh = FakeGitHub([Candidate(99, "X", "u")])
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == []


def test_slot_exhaustion_stops_the_claim_round(tmp_path, monkeypatch):
    """Box-wide capacity is free (free > 0), but allocate_slot has nothing to
    hand out — the round must stop rather than claim a candidate it cannot
    give a slot to."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    monkeypatch.setattr(main, "allocate_slot", lambda *a, **kw: None)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])

    main._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False)

    assert gh.claimed == []


def test_active_task_for_an_unconfigured_repository_does_not_break_the_round(
        tmp_path, monkeypatch):
    """A repository that has since left targets.yaml can still have an
    in-flight task on disk. _round_counts must not crash looking it up, and
    the round must still claim normally for the repositories still configured."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    orphan_wt = Path(c.repositories[0].worktrees_path) / "task-orphan-1"
    (orphan_wt / ".agent").mkdir(parents=True, exist_ok=True)
    save(c.state_dir, TaskState(
        issue=1, repository="retired_repository", stage=Stage.IMPLEMENT, slot=0,
        worktree=str(orphan_wt), branch="agent/task-1", title="t",
        updated_at="2026-07-21T00:00:00+00:00", track="standard"))
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])

    main._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False)

    assert gh.claimed == [42]


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
    from dispatcher import intents
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


def test_workspace_failure_reports_quarantines_and_pass_survives(tmp_path, monkeypatch):
    """create_workspace failure: no claim, no state file, one issue + one
    ping, quarantine written, and the pass exits cleanly (regression: one
    bad candidate killed the pass). Capped at one provisioning failure per
    repository per pass, so the NEXT candidate (43) is NOT claimed this pass —
    a systemic fault is far likelier than a per-candidate one, and the next
    pass retries 43."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")])
    sess = FakeSessions()

    def failing_workspace(repository, issue, base="main", dry_run=False, auth=None):
        if issue == 42:
            raise RuntimeError("pipenv: no python 3.13")
        wt = Path(repository.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(main, "create_workspace", failing_workspace)
    d = deps(gh, sess)
    main.run_pass(c, d)  # must NOT raise

    assert gh.claimed == [], "capped at one provisioning failure per pass; 43 not claimed this pass"
    assert load(c.state_dir, "portfolio_eval", 42) is None, "no state file for the failed candidate"
    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/agent-ops" and "provisioning" in title
    assert "no python 3.13" in body
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501 and rec["blocker_repo"] == "jesdi/agent-ops"


def test_workspace_failure_second_pass_dedupes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Bad", "u42")])

    def failing_workspace(repository, issue, base="main", dry_run=False, auth=None):
        raise RuntimeError("pipenv: no python 3.13")

    monkeypatch.setattr(main, "create_workspace", failing_workspace)
    d = deps(gh, FakeSessions())
    main.run_pass(c, d)
    # Simulate a force-retry (record deleted) with the same failure: the
    # fingerprint dedupes, and the quarantine is rewritten with the
    # ORIGINAL blocker number read back from the marker.
    failures.quarantine_path(c.state_dir, "portfolio_eval", 42).unlink()
    main.run_pass(c, d)
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1
    rec = json.loads(failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).read_text())
    assert rec["blocker_issue"] == 501


def test_quarantined_candidate_with_open_blocker_skipped(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []


def test_closed_blocker_unquarantines_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Fixed now", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "CLOSED"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42], "candidate claimable in the SAME pass"
    assert not failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).exists()


def test_issue_state_error_keeps_candidate_quarantined(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Bad", "u42"), Candidate(43, "Good", "u43")],
                    issue_state_raises=True)
    main.run_pass(c, deps(gh, FakeSessions()))  # must not raise
    assert gh.claimed == [43]


def test_quarantine_verdict_tracks_an_open_blocker_and_pass_heartbeat(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Blocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["portfolio_eval", 42]]}
    assert gh.claimed == []


def test_quarantine_verdict_clears_a_closed_blocker_same_pass(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    verdict_path.write_text(json.dumps({
        "pass_started": "2026-11-15T09:50:00+00:00",
        "skipped": [["portfolio_eval", 42]],
    }))
    gh = FakeGitHub([Candidate(42, "Unblocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "CLOSED"})

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(verdict_path.read_text())
    assert verdict == {"pass_started": heartbeat["started_at"], "skipped": []}
    assert gh.claimed == [42]
    assert not failures.quarantine_path(c.state_dir, "portfolio_eval", 42).exists()


def test_quarantine_verdict_lists_a_record_without_a_blocker(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    no_blocker = failures.quarantine_path(c.state_dir, "portfolio_eval", 42)
    no_blocker.parent.mkdir(parents=True, exist_ok=True)
    no_blocker.write_text(json.dumps({
        "task_issue": 42, "blocker_repo": "jesdi/agent-ops",
        "blocker_issue": 0, "fingerprint": "abc", "created_at": "now",
    }))
    gh = FakeGitHub([Candidate(42, "No blocker reference", "u42")])

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["portfolio_eval", 42]]}
    assert gh.claimed == []


def test_quarantine_verdict_lists_unreadable_and_failed_lookup_candidates(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    alpha = dc_replace(
        c.repositories[0], name="alpha", repo="jesdi/alpha",
        clone_path=str(tmp_path / "alpha"),
        worktrees_path=str(tmp_path / "alpha.worktrees"),
        project=dc_replace(c.repositories[0].project, name="alpha"))
    beta = dc_replace(
        c.repositories[0], name="beta", repo="jesdi/beta",
        clone_path=str(tmp_path / "beta"),
        worktrees_path=str(tmp_path / "beta.worktrees"),
        project=dc_replace(c.repositories[0].project, name="beta"))
    c = dc_replace(c, repositories=[alpha, beta], capacity=2)
    bad_record = failures.quarantine_path(c.state_dir, "alpha", 73)
    bad_record.parent.mkdir(parents=True, exist_ok=True)
    bad_record.write_text("{")
    failures.write_quarantine(c.state_dir, "beta", 12,
                              "jesdi/agent-ops", 502, "def")
    gh = FakeGitHub(
        issue_state_raises=True,
        cands_by_repository={
            "alpha": [Candidate(73, "Unreadable quarantine", "u73")],
            "beta": [Candidate(12, "Failed blocker lookup", "u12")],
        })

    main.run_pass(c, deps(gh, FakeSessions()))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"],
                       "skipped": [["alpha", 73], ["beta", 12]]}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_capacity_is_full(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    for slot, issue in enumerate((1, 2, 3)):
        wt = Path(c.repositories[0].worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True)
        save(c.state_dir, TaskState(
            issue=issue, repository="portfolio_eval", stage=Stage.SPEC,
            slot=slot, worktree=str(wt), branch=f"agent/task-{issue}",
            title="t", updated_at="2026-07-14T00:00:00+00:00",
            track="standard"))
    gh = FakeGitHub([Candidate(99, "No room", "u99")])

    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": heartbeat["started_at"], "skipped": []}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_slot_allocation_exhausts(
        tmp_path, monkeypatch):
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 73,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub(
        [Candidate(73, "Blocked", "u73"), Candidate(74, "No slot", "u74")],
        issue_states={("jesdi/agent-ops", 501): "OPEN"})
    monkeypatch.setattr(main, "allocate_slot", lambda *a, **kw: None)
    pass_started = "2026-11-15T10:00:00+00:00"

    main._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False,
                    pass_started)

    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": pass_started,
                       "skipped": [["portfolio_eval", 73]]}
    assert gh.claimed == []


def test_quarantine_verdict_is_written_when_no_project_remains_to_pick(
        tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[with_project(
        c.repositories[0], max_active=1)], capacity=2)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 73,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub(
        [Candidate(73, "Blocked", "u73"), Candidate(74, "Claimed", "u74")],
        issue_states={("jesdi/agent-ops", 501): "OPEN"})
    pass_started = "2026-11-15T10:00:00+00:00"

    main._claim_new(c, deps(gh), c.repositories, ADMIT_ALL, tuple, False,
                    pass_started)

    verdict = json.loads(
        (Path(c.state_dir) / "quarantine-verdict.json").read_text())
    assert verdict == {"pass_started": pass_started,
                       "skipped": [["portfolio_eval", 73]]}
    assert gh.claimed == [74]


def test_quarantine_verdict_write_failure_warns_without_breaking_pass(
        tmp_path, monkeypatch, capsys):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.mkdir(parents=True)

    d = deps()
    main.run_pass(c, d)

    captured = capsys.readouterr()
    assert "[warn]" in captured.err and "quarantine verdict" in captured.err.lower()
    assert (Path(c.state_dir) / "pass.json").is_file()


def test_claims_paused_keeps_the_verdict_file_stale_and_untrusted(
        tmp_path, monkeypatch):
    from dispatcher import triage
    from web.sources import Sources

    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    verdict_path = Path(c.state_dir) / "quarantine-verdict.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    prior = (b'{"pass_started":"2026-11-15T09:50:00+00:00",'
             b'"skipped":[["portfolio_eval",42]]}')
    verdict_path.write_bytes(prior)
    (Path(c.state_dir) / triage.REQUEST_FILE).write_text(
        '{"requested_at":"2026-11-15T09:50:00+00:00"}')
    # The request is still waiting for capacity: tick neither launches nor
    # expires it, so the pass sees claims paused.
    monkeypatch.setattr(triage, "tick", lambda *_args: None)

    d = deps()
    main.run_pass(c, d)

    heartbeat = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert verdict_path.read_bytes() == prior
    assert heartbeat["started_at"] != "2026-11-15T09:50:00+00:00"
    assert Sources(c.state_dir, d.sessions, d.github).quarantine_verdict(
        heartbeat) == frozenset()


def test_run_pass_verdict_is_readable_against_its_pass_heartbeat(
        tmp_path, monkeypatch):
    from web.sources import Sources

    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    gh = FakeGitHub([Candidate(42, "Blocked", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})

    d = deps(gh, FakeSessions())
    main.run_pass(c, d)

    sources = Sources(c.state_dir, d.sessions, d.github)
    assert sources.quarantine_verdict(sources.pass_heartbeat()) == frozenset({
        ("portfolio_eval", 42)})


def test_release_called_when_claim_fails(tmp_path, monkeypatch):
    """github.claim failure after workspace+save must trigger release and leave QUEUED state (regression: orphan-claim window)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)

    class ClaimFailsGitHub(FakeGitHub):
        def claim(self, repository, cand):
            raise RuntimeError("GitHub API error")

    gh = ClaimFailsGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()

    with pytest.raises(RuntimeError):
        main.run_pass(c, deps(gh, sess))

    assert any(issue == 42 for issue, _ in gh.released), "release must be called for the issue when claim fails"
    ts = load(c.state_dir, "portfolio_eval", 42)
    assert ts is not None and ts.stage is Stage.QUEUED, "QUEUED state file must exist for crash-recovery path"


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


def test_ci_completion_marks_unpark_requested(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, status=ci(4242))
    gh = FakeGitHub(run_conclusion="failure")
    # No free capacity race: resumed same pass is fine — assert either state.
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind in (Kind.WAKING, Kind.RUNNING)  # woken; may already have resumed
    from dispatcher import messages
    # Message may be delivered already if resumed same pass; all_messages captures both.
    assert "run 4242 concluded: failure" in messages.all_messages(
        c.state_dir, "portfolio_eval", 42)[0].text


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


from telegram.inbound import Command, Plain, Reply


def test_reply_wakes_matching_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55, text="use oauth")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "use oauth"


def test_reply_to_unknown_message_is_reported(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    patch_events(monkeypatch, [Reply(reply_to_msg_id=999, text="hi")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent  # "(reply didn't match any parked task)"


def test_plain_text_with_single_parked_task_wakes_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="go ahead")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_plain_text_with_two_parked_tasks_asks_which(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, status=human(), park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.PARKED_HUMAN
    assert "status" in d.notifier.sent  # the "Which task?" prompt


def test_attach_command_sets_hold(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.hold_for_attach is True


def test_attach_with_an_ambiguous_issue_number_wakes_nothing(tmp_path, monkeypatch):
    from tests.test_mpb_t03_messages_and_markers import (make_task_for,
                                                          two_repository_cfg)
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)
    make_task_for(c, "portfolio_eval", 7, status=human(), park_msg_id=55)
    make_task_for(c, "factorial", 7, status=human(), park_msg_id=56)
    patch_events(monkeypatch, [Command(name="attach", issue=7)])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 7).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "factorial", 7).status.kind is Kind.PARKED_HUMAN
    lines = [ctx["lines"] for tmpl, ctx in d.notifier.calls
             if tmpl == "status" and "ambiguous" in ctx["lines"][0]][0]
    assert sorted(lines[1:]) == ["factorial#7", "portfolio_eval#7"]


def test_attach_on_an_unparked_issue_says_so(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    d = deps(sess=FakeSessions(alive={42}))
    main.run_pass(c, d)
    assert ("status", {"lines": ["#42 is not parked"]}) in d.notifier.calls


def test_attach_command_queues_no_message(tmp_path, monkeypatch):
    """/attach is a wake, not a message. The empty text it hands _wake must
    not land in the queue: a zero-length message shows a phantom ✉ badge on
    the card, an empty row in the thread, and appends a junk "Operator
    messages" block with an empty entry to the resume prompt."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)  # the running task holds the only capacity unit
    patch_events(monkeypatch, [Command(name="attach", issue=42)])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    from dispatcher import messages
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.hold_for_attach is True
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []


def test_status_command_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(7))
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent


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


def test_run_status_error_does_not_abort_pass(tmp_path, monkeypatch):
    """A gh run_status failure must be treated as 'still running': skip that task,
    keep the pass going (e.g., a second claimable candidate is still claimed)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    # Parked CI task whose run_status call will raise CalledProcessError
    make_task(c, issue=42, status=ci(4242))
    # A fresh candidate available to be claimed
    gh = FakeGitHub(cands=[Candidate(99, "fresh", "u")], run_status_raises=True)
    sess = FakeSessions()
    # Must NOT raise; the pass must survive the error
    main.run_pass(c, deps(gh, sess))
    # CI-parked task remains unchanged (still PARK_CI with same run_id)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_CI and t.status.run_id == 4242
    # The rest of the pass continued: the fresh candidate was claimed
    assert gh.claimed == [99]


def test_queue_command_sends_ranked_view(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(2, title="B", score=5.0, boost=1),
                          row(1, title="A", score=1.0),
                          row(3, title="C", score=None),
                          row(4, title="D", blocked=True),
                          row(5, title="E", status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    template, ctx = d.notifier.contexts[0]
    assert template == "queue"
    lines = ctx["lines"]
    assert lines[0] == "1. ↑1 [5.00] #2 B"
    assert lines[1] == "2. [1.00] #1 A"
    assert lines[2] == "3. [—] #3 C"
    assert "In progress: #5" in lines
    assert "Blocked: #4" in lines


def test_boost_command_adjusts_and_confirms(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, boost=2)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=-3)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, -1)]
    assert any("#7 boost 2 → -1" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_unknown_issue_replies_not_on_board(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=99, amount=1)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    assert any("#99 is not on the board" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_eligible_sets_head_boost(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [(7, 99)]
    assert gh.statused == [] and gh.labeled == []


def test_next_ineligible_refuses_with_reason_and_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "not eligible" in joined and "Backlog" in joined
    assert "auto" in joined and "/next 7 force" in joined


def test_next_force_flips_status_and_label_then_boosts(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.statused == [(7, "Ready")]
    assert gh.labeled == [(7, "auto")]
    assert gh.boosts == [(7, 99)]


def test_next_blocked_never_forced(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, blocked=True)])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("blocked" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_boost_ambiguous_across_repositories_asks_to_disambiguate(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    other = dc_replace(c.repositories[0], name="other",
                       project=dc_replace(c.repositories[0].project, name="other"))
    c = dc_replace(c, repositories=[c.repositories[0], other])
    gh = FakeGitHub(rows=[row(7)])   # rank_rows returns #7 for BOTH projects
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(c, d)
    assert gh.boosts == []
    assert any("multiple repositories" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_gh_failure_reports_error_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.boost_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_boost_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="boost", issue=7, amount=1)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("boost failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_lookup_error_next_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7)])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_on_a_board_missing_a_name_reports_it(tmp_path, monkeypatch):
    patch_usage(monkeypatch)

    class NoReady(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise BoardError("project jesdi/1: field 'Status' has no option 'Ready'")

    gh = NoReady(rows=[row(7, status="Backlog")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("#7 next failed: project jesdi/1: field 'Status' has no option "
               "'Ready'" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_command_value_error_queue_reports_and_pass_survives(tmp_path, monkeypatch):
    patch_usage(monkeypatch)

    def bad_rank_rows(repositories):
        raise ValueError("malformed JSON from rank output")

    gh = FakeGitHub(rows=[row(7)])
    monkeypatch.setattr(gh, "rank_rows", bad_rank_rows)
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert any("/queue failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_in_progress_refused_without_force_hint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress")])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    joined = " ".join(l for _, ctx in d.notifier.contexts
                      for l in ctx.get("lines", []))
    assert "In progress" in joined
    assert "force" not in joined.replace("cannot be forced", "")


def test_next_in_progress_never_forced(tmp_path, monkeypatch):
    """The board is the double-dispatch guard: /next force must never flip a
    claimed issue back to Ready."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="In progress", labels=())])
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("In progress" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_next_force_boost_failure_leaves_board_unmutated(tmp_path, monkeypatch):
    """set_boost runs first, so a Boost-field failure is a clean no-op."""
    patch_usage(monkeypatch)
    gh = FakeGitHub(rows=[row(7, status="Backlog", labels=())])
    gh.lookup_raises = True
    d = deps(gh)
    patch_events(monkeypatch, [Command(name="next", issue=7, force=True)])
    main.run_pass(cfg(tmp_path), d)   # must not raise
    assert gh.boosts == [] and gh.statused == [] and gh.labeled == []
    assert any("#7 next failed" in l
               for _, ctx in d.notifier.contexts for l in ctx.get("lines", []))


def test_queue_shows_demote_marker_and_truncates(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    rows = [row(1, title="A", score=9.0, boost=-3)]
    rows += [row(n, title=f"T{n}", score=1.0) for n in range(2, 14)]
    d = deps(FakeGitHub(rows=rows))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(cfg(tmp_path), d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines[0] == "1. ↓3 [9.00] #1 A"
    assert len([l for l in lines if l[0].isdigit()]) == 10
    assert "… 3 more" in lines


def test_queue_labels_each_project_and_reports_empty(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0], with_project(
        dc_replace(c.repositories[0], name="other"), name="other")])
    d = deps(FakeGitHub(rows=[]))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(c, d)
    lines = d.notifier.contexts[0][1]["lines"]
    assert lines == ["[portfolio_eval]", "(queue empty)",
                     "[other]", "(queue empty)"]


def test_queue_names_the_repository_in_a_project_of_several(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    first = c.repositories[0]
    c = dc_replace(c, repositories=[first, dc_replace(first, name="other")])
    d = deps(FakeGitHub(rows=[row(2, title="B", score=5.0),
                              {**row(3, title="C", score=1.0), "repository": "other"},
                              {**row(4, blocked=True), "repository": "other"}]))
    patch_events(monkeypatch, [Command(name="queue")])
    main.run_pass(c, d)
    assert d.notifier.contexts[0][1]["lines"] == [
        "1. [5.00] portfolio_eval#2 B", "2. [1.00] other#3 C", "Blocked: other#4"]


def test_status_counts_the_slot_the_triage_sweep_holds(tmp_path, monkeypatch):
    """The sweep's work is a tmux session, not a TaskState, so active() cannot
    see it — but _run_pass reduces effective capacity for it. Without counting
    it here, /status reports 'capacity 1/2' on a full box for the whole sweep,
    which is the operator's primary visibility surface lying."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.IMPLEMENT, slot=0,
        worktree="/wt", branch="agent/task-42", title="Fix rounding",
        updated_at="2026-07-24T00:00:00+00:00"))
    assert main._status_lines(c)[-1] == f"capacity 1/{c.capacity}"
    monkeypatch.setattr(main.triage, "running", lambda: True)
    lines = main._status_lines(c)
    assert lines[-1] == f"capacity 2/{c.capacity}"
    assert "triage sweep running (holds 1 slot)" in lines


def test_status_reply_content_carries_the_status_lines(tmp_path, monkeypatch):
    """Finding C: FakeNotifier previously recorded only the template name, so
    no test could assert on what a /status reply actually contains."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(7))
    patch_events(monkeypatch, [Command(name="status")])
    d = deps()
    main.run_pass(c, d)
    template, ctx = [call for call in d.notifier.calls if call[0] == "status"][-1]
    assert any(line.startswith("#42 ") for line in ctx["lines"])


def test_digest_content_carries_the_status_lines(tmp_path):
    """Finding C: the digest's payload was likewise never asserted on."""
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.PLAN, gated=True, slot=0,
                                worktree="/x", branch="b", title="t",
                                updated_at="2026-07-14T00:00:00+00:00"))
    d = deps()
    main.send_digest(c, d)
    template, ctx = d.notifier.calls[-1]
    assert template == "daily_digest"
    assert any(line.startswith("#42 ") for line in ctx["lines"])


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


def test_claim_and_spawn_append_claimed_and_stage_started_events(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    main.run_pass(c, deps(FakeGitHub([Candidate(42, "Add widget", "u42")])))
    events = eventlog.read_tail(c.state_dir)
    assert [(e["event"], e["issue"]) for e in events] == [
        ("claimed", 42), ("stage-started", 42)]
    assert events[0]["repository"] == "portfolio_eval"
    assert events[1]["stage"] == "spec"
    assert events[1]["model"] == "anthropic/claude-opus-5"


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


def test_apply_intent_resolves_by_repository(tmp_path):
    """A repository-carrying kill intent must resolve to exactly that repository's
    task — even when a different repository happens to have a task with the
    identical issue number. Regression for the (repository, issue) rekey:
    _task_for_intent looks up by (repository, issue), not by issue alone, so
    killing agent_ops#42 must leave portfolio_eval#42 completely untouched."""
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0],
                                    dc_replace(c.repositories[0], name="agent_ops",
                                               repo="jesdi/agent-ops")])
    make_task(c, issue=42)  # portfolio_eval#42 — make_task's default repository
    wt2 = Path(c.repositories[1].worktrees_path) / "task-42"
    (wt2 / ".agent").mkdir(parents=True, exist_ok=True)
    save(c.state_dir, TaskState(
        issue=42, repository="agent_ops", stage=Stage.IMPLEMENT, slot=1,
        worktree=str(wt2), branch="agent/task-42", title="t",
        updated_at="2026-07-21T00:00:00+00:00"))
    intents_mod.write_intent(c.state_dir, "kill", "agent_ops", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main._apply_intents(c, deps(sess=sess))

    killed = load(c.state_dir, "agent_ops", 42)
    assert killed.status.kind is Kind.FAILED

    untouched = load(c.state_dir, "portfolio_eval", 42)
    assert untouched.stage is Stage.IMPLEMENT
    assert untouched.status.kind is Kind.RUNNING


def test_reply_intent_wakes_parked_task_and_is_deleted(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot → 42 stays waking
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                             actor="jesdi@github", epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "use oauth"
    assert intents_mod.list_intents(c.state_dir) == []
    applied = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "intent-applied"]
    assert applied[0]["actor"] == "jesdi@github"
    assert applied[0]["detail"] == "reply" and applied[0]["issue"] == 42
    assert applied[0]["repository"] == "portfolio_eval"


def test_intent_applied_event_carries_repository_resolved_from_legacy_intent(tmp_path, monkeypatch):
    """A legacy intent file (repository=="", predates repository-keying) carries no
    repository of its own, but if it resolves unambiguously to one task, the
    intent-applied event must carry THAT task's repository — not "" — so the
    console's timeline link (/task/{repository}/{issue}) is never blank."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)  # portfolio_eval#42 — make_task's default repository
    intents_mod.write_intent(c.state_dir, "reply", "", 42, {"text": "hi"},
                             "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    applied = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "intent-applied"]
    assert applied[0]["issue"] == 42
    assert applied[0]["repository"] == "portfolio_eval"


def test_legacy_reply_intent_with_no_task_and_several_repositories_is_dropped(
        tmp_path, monkeypatch, capsys):
    """A repository-less reply for an issue with no task cannot be attributed
    when several repositories are configured: it is dropped with a warning, never
    queued under a guessed project."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0],
                                    dc_replace(c.repositories[0], name="factorial")])
    intents_mod.write_intent(c.state_dir, "reply", "", 42, {"text": "hi"},
                             "op", 1)
    main._apply_intents(c, deps())
    mdir = Path(c.state_dir) / "messages"
    assert not mdir.exists() or list(mdir.iterdir()) == []
    assert "#42" in capsys.readouterr().err


def test_reply_intent_for_running_task_is_queued_but_task_stays_running(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)  # not parked — session is live
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "hi"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    # message is queued — not dropped — and the task stays unparked
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "hi"
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING  # no wake flip for a live session
    assert intents_mod.list_intents(c.state_dir) == []  # intent deleted


def test_reply_intent_on_gate_parked_task_wakes_it(tmp_path, monkeypatch):
    # The console SpecPanel "approve" button sends a `reply` intent; the task
    # is parked for review (not for a question), so _apply_one_intent must accept it.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot → 42 stays waking
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "Approved — proceed."}, actor="jesdi@github",
                             epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "Approved — proceed."
    assert intents_mod.list_intents(c.state_dir) == []


def _reply_pass(c, text, sess):
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": text},
                             "jesdi@github", 1)
    main.run_pass(c, deps(sess=sess))
    return load(c.state_dir, "portfolio_eval", 42)


def test_reply_while_the_gate_session_is_alive_ends_it_and_wakes_through_review(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    page = PlanApprovalRequest(".agent/review.html", fingerprint="fp")
    make_task(c, issue=42, stage=Stage.PLAN, status=held(page), slot=2)
    make_task(c, issue=43)                      # holds the only capacity unit
    sess = FakeSessions(alive={42, 43})
    t = _reply_pass(c, "split ticket 3", sess)
    assert 42 in sess.ended and t.slot == NO_SLOT
    assert (t.stage, t.status) == (Stage.PLAN, waking(review(page)))
    [e] = [e for e in _events(c, "transition") if e["transition"]["event"] == "op-reply"]
    assert e["actor"] == "jesdi@github"


def test_answers_request_survives_a_blocked_wake(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    request = AnswersRequest(".agent/questions.html", fingerprint="fp")
    make_task(c, issue=42, stage=Stage.SPEC, status=answers(request), slot=NO_SLOT)
    make_task(c, issue=43)                      # the wake is blocked on capacity
    t = _reply_pass(c, "see my answers", FakeSessions(alive={43}))
    assert t.status == waking(answers(request))
    assert operator_request(t) == request and operator_request(t).fingerprint == "fp"


def test_a_rejected_reply_keeps_its_message(tmp_path, monkeypatch):
    """Answered 202 on a loop-cap park; the pass in progress moved the task
    to failed(pr-closed), so the next pass refuses the reply out loud."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=failed("pr-closed"), slot=NO_SLOT,
              updated_at=_now())    # inside the retention window
    before = load(c.state_dir, "portfolio_eval", 42)
    assert _reply_pass(c, "try once more", FakeSessions()) == before
    [e] = _events(c, "transition-rejected")
    assert e["transition"]["event"] == "op-reply"
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "try once more"]


def test_a_reply_for_an_issue_with_no_task_file_is_queued(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    assert _reply_pass(c, "hello", FakeSessions()) is None
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == ["hello"]
    assert _events(c, "transition") == _events(c, "transition-rejected") == []


def test_park_intent_parks_a_live_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "park", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == human("operator", "parked by operator")
    assert sess.ended == [42]
    assert "parked_question" not in d.notifier.sent   # the operator parked it: nothing to ask
    assert intents_mod.list_intents(c.state_dir) == []


def test_kill_intent_ends_releases_and_tombstones_state(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess))
    assert sess.ended == [42]
    assert gh.released == [(42, "abandoned by operator")]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert not has_waiting(c.state_dir, "portfolio_eval", 42)
    failed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "failed"]
    assert failed[0]["detail"] == "killed by operator"


def test_kill_intent_survives_release_failure(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)

    class ReleaseFailsGitHub(FakeGitHub):
        def release(self, repository, issue, reason):
            raise RuntimeError("gh outage")

    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(ReleaseFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED  # tombstone written even when release raises


def test_kill_intent_does_not_reclaim_released_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    # release() flips #42 back to Ready+auto, so it is still a live board
    # candidate this pass; the FAILED tombstone must stop _claim_new from
    # re-claiming the issue the operator just killed.
    gh = FakeGitHub([Candidate(42, "Killed but still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions(alive={42})))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED


def test_kill_intent_with_repository_ends_a_stray_session_with_no_state_file(tmp_path):
    """A repository-carrying kill intent must be able to end a session even when
    there is no task file to resolve — e.g. the flush deleted a DONE
    task's state while its tmux session leaked (never actually ended). The
    operator's kill intent still names its repository, so nothing stops the
    session from being ended even though there is no task left to
    tombstone."""
    c = cfg(tmp_path)
    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main._apply_intents(c, deps(sess=sess))
    assert sess.end_calls == [("portfolio_eval", 42)]
    assert load(c.state_dir, "portfolio_eval", 42) is None  # nothing to tombstone


def test_cancel_intent_ends_session_moves_board_and_tombstones(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess))
    assert sess.ended == [42]
    assert gh.canceled == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED
    assert not has_waiting(c.state_dir, "portfolio_eval", 42)
    ev = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "canceled"]
    assert ev[0]["detail"] == "canceled by operator"


def test_cancel_intent_survives_github_failure(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)

    class CancelFailsGitHub(FakeGitHub):
        def cancel(self, repository, issue):
            raise RuntimeError("gh outage")

    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(CancelFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED


def test_cancel_intent_does_not_reclaim_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    # The board write can lag (or fail): #42 may still rank as a candidate
    # this pass; the CANCELED tombstone must stop _claim_new.
    gh = FakeGitHub([Candidate(42, "Canceled but still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions(alive={42})))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED


def test_cancel_tombstone_releases_the_slot_immediately(tmp_path, monkeypatch):
    # Same contract as parking: a transition that ends the session gives the
    # slot back at transition time, not a pass later —
    # the web capacity view reads the state file as-is.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=1)
    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.CANCELED
    assert t.slot == NO_SLOT


def test_reopened_canceled_issue_is_claimed_again(tmp_path, monkeypatch):
    # Won't-do is reversible: the operator reopens the issue and moves the
    # card back to Ready, so it ranks as a candidate again (rank drops CLOSED
    # issues — a candidate row proves the reopen). The stale CANCELED
    # tombstone must be swept and the issue claimed fresh.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=CANCELED, slot=NO_SLOT,
              updated_at="2026-07-21T00:00:00+00:00")  # canceled long ago
    gh = FakeGitHub([Candidate(42, "Conditions changed", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is not Kind.CANCELED
    reopened = [e for e in eventlog.read_tail(c.state_dir)
                if e["event"] == "reopened"]
    assert reopened and reopened[0]["issue"] == 42
    claimed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "claimed"]
    assert claimed and claimed[0]["stage"] == "queued"


@pytest.mark.parametrize("reason", ["pr-closed", "unknown"])
def test_failed_queued_task_ranked_again_is_swept_and_claimed(tmp_path, monkeypatch, reason):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, status=failed(reason), slot=NO_SLOT,
              updated_at="2026-07-21T00:00:00+00:00")
    old = load(c.state_dir, "portfolio_eval", 42)
    gh = FakeGitHub([Candidate(42, "Ranked again", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == [42]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is not Kind.FAILED and t.title != old.title
    events = eventlog.read_tail(c.state_dir)
    reopened = [e for e in events if e["event"] == "reopened"]
    assert [e["issue"] for e in reopened] == [42]
    assert reopened[0]["detail"] == "failed task ranked again"
    claimed = [e for e in events if e["event"] == "claimed"]
    assert claimed and claimed[0]["stage"] == "queued"


def test_canceled_this_pass_ranks_without_sweeping_its_tombstone(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=CANCELED, slot=NO_SLOT,
              updated_at="2999-01-01T00:00:00+00:00")   # written after the pass started
    gh = FakeGitHub([Candidate(42, "Still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED
    assert not [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "reopened"]


def test_failed_task_ranked_again_wakes_in_place(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    made = []
    monkeypatch.setattr(main, "create_workspace", lambda *a, **k: made.append(a))
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, status=failed("task-crash"), slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Failure issue closed", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == waking(failed("task-crash"), "ranked")
    assert t.stage is Stage.REVIEW
    assert gh.claimed == [] and made == []
    assert [x.issue for x in load_all(c.state_dir)] == [42]


def test_failed_this_pass_ranks_without_waking(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, status=failed("task-crash"), slot=NO_SLOT,
              updated_at="2999-01-01T00:00:00+00:00")
    gh = FakeGitHub([Candidate(42, "Still on board", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert load(c.state_dir, "portfolio_eval", 42).status == failed("task-crash")
    assert gh.claimed == []


def test_done_task_ranked_again_is_rejected(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=DONE, slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Merged but Ready", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert gh.claimed == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE
    rejected = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "transition-rejected"]
    assert rejected and rejected[0]["transition"]["event"] == "candidate-ranked"


def test_done_task_ranked_every_pass_is_rejected_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PR_OPEN, status=DONE, slot=NO_SLOT,
              updated_at=_ago(minutes=1))
    gh = FakeGitHub([Candidate(42, "Merged but Ready", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    main.run_pass(c, deps(gh, FakeSessions()))
    assert len(_events(c, "transition-rejected")) == 1
    t = load(c.state_dir, "portfolio_eval", 42)
    save(c.state_dir, dc_replace(t, updated_at=_ago(seconds=1)))  # the task changed
    main.run_pass(c, deps(gh, FakeSessions()))
    assert len(_events(c, "transition-rejected")) == 2


def test_cancel_intent_without_task_file_uses_intent_repository(tmp_path, monkeypatch):
    # A backlog card has no task file; the intent itself names the repository
    # so the board/issue side still happens. No tombstone is written.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 99,
                             {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert gh.canceled == [99]
    assert sess.end_calls == [("portfolio_eval", 99)]
    assert load(c.state_dir, "portfolio_eval", 99) is None


def test_legacy_cancel_intent_with_no_matching_task_is_skipped(tmp_path, monkeypatch):
    # A pre-repository intent file names no repository; with no unique task to
    # borrow one from there is no board to write and no session to end.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    intents_mod.write_intent(c.state_dir, "cancel", "", 99, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert gh.canceled == []
    assert sess.end_calls == []


def _operator_intent_pass(c, action, sess=None, gh=None):
    intents_mod.write_intent(c.state_dir, action, "portfolio_eval", 42, {}, "jesdi@github", 1)
    main.run_pass(c, deps(gh or FakeGitHub(), sess or FakeSessions()))
    return load(c.state_dir, "portfolio_eval", 42)


def test_kill_ends_the_session_releases_the_card_and_fails_the_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT)
    gh, sess = FakeGitHub(), FakeSessions(alive={42})
    t = _operator_intent_pass(c, "kill", sess, gh)
    assert (t.stage, t.status) == (Stage.IMPLEMENT, failed("killed"))
    assert sess.ended == [42] and gh.released == [(42, "abandoned by operator")]
    [e] = [e for e in _events(c, "transition") if e["transition"]["event"] == "op-kill"]
    assert e["actor"] == "jesdi@github"


def test_cancel_moves_the_card_to_wont_do_and_cancels_the_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=human("decision"), slot=NO_SLOT)
    gh = FakeGitHub()
    t = _operator_intent_pass(c, "cancel", gh=gh)
    assert (t.stage, t.status) == (Stage.SPEC, CANCELED)
    assert gh.canceled == [42]
    assert [e["transition"]["event"] for e in _events(c, "transition")] == ["op-cancel"]


def test_park_intent_on_a_task_that_is_not_running_is_rejected(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PLAN, status=human("operator"), slot=NO_SLOT)
    before = load(c.state_dir, "portfolio_eval", 42)
    assert _operator_intent_pass(c, "park") == before
    [e] = _events(c, "transition-rejected")
    assert e["transition"]["event"] == "op-park"


def test_a_park_intent_the_pass_overtook_is_rejected_and_stays_parked_on_the_pr(
        tmp_path, monkeypatch):
    """Accepted by the web on a running review; by the time the pass applies
    it the task is parked on its PR, so the pass refuses it out loud."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN, status=PR, slot=NO_SLOT)
    sess = FakeSessions()
    t = _operator_intent_pass(c, "park", sess)
    assert (t.stage, t.status) == (Stage.PR_OPEN, PR)
    assert sess.ended == []
    [e] = _events(c, "transition-rejected")
    assert (e["transition"]["event"], e["actor"]) == ("op-park", "jesdi@github")


def test_an_intent_of_an_unknown_action_is_warned_and_deleted(tmp_path, capsys):
    c = cfg(tmp_path)
    p = intents_mod.write_intent(c.state_dir, "park", "portfolio_eval", 42, {}, "op", 1)
    p.write_text(json.dumps({**json.loads(p.read_text()), "action": "bogus"}))
    main._apply_intents(c, deps())
    assert "unknown intent action 'bogus'" in capsys.readouterr().err
    assert intents_mod.list_intents(c.state_dir) == []


def test_retry_intent_clears_quarantine_and_fingerprint(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    failures.write_quarantine(c.state_dir, "portfolio_eval", 42,
                              "jesdi/agent-ops", 501, "abc")
    failures.fingerprint_path(c.state_dir, "abc").parent.mkdir(
        parents=True, exist_ok=True)
    failures.fingerprint_path(c.state_dir, "abc").write_text(
        json.dumps({"repo": "jesdi/agent-ops", "issue": 501, "when": "w"}))
    intents_mod.write_intent(c.state_dir, "retry", "portfolio_eval", 42, {}, "op", 1)
    gh = FakeGitHub([Candidate(42, "Retry me", "u42")],
                    issue_states={("jesdi/agent-ops", 501): "OPEN"})
    main.run_pass(c, deps(gh, FakeSessions()))
    assert not failures.quarantine_path(
        c.state_dir, "portfolio_eval", 42).exists()
    assert not failures.fingerprint_path(c.state_dir, "abc").exists()
    assert gh.claimed == [42], "cleared before _claim_new → claimable same pass"


def test_retry_intent_clears_a_symlink_loop_record(tmp_path, monkeypatch):
    """exists() hides ELOOP as False; retry must still unlink the record."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    qp = failures.quarantine_path(c.state_dir, "portfolio_eval", 42)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.symlink_to(qp)
    intents_mod.write_intent(c.state_dir, "retry", "portfolio_eval", 42, {}, "op", 1)
    gh = FakeGitHub([Candidate(42, "Retry me", "u42")])
    main.run_pass(c, deps(gh, FakeSessions()))
    assert not qp.is_symlink()
    assert gh.claimed == [42]


def test_resume_intent_wakes_with_default_text(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=human(), park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    # The wake's launch writes the text itself: nothing is queued for it.
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []
    assert "The operator resumed this task. Continue." in sess.resumed[0][1]


def test_resume_intent_carries_optional_text(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=human(), park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {"text": "ship it"},
                             "op", 1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    # The wake's launch writes the text itself: nothing is queued for it.
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []
    assert "ship it" in sess.resumed[0][1]


def test_resume_intent_with_an_unconfigured_model_is_dropped_with_an_event(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    # A legacy intent (no repository): the event still names the task's repository.
    intents_mod.write_intent(c.state_dir, "resume", "", 42,
                             {"model": "openai/gpt-nope"}, "op", 1)
    main._apply_intents(c, deps())

    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    [event] = [e for e in eventlog.read_tail(c.state_dir) if e["issue"] == 42]
    assert (event["event"], event["repository"], event["model"]) == (
        "intent-dropped", "portfolio_eval", "openai/gpt-nope")
    assert event["detail"] == (
        "model 'openai/gpt-nope' is not configured for repository 'portfolio_eval'")


def test_resume_intent_can_override_a_wake_without_duplicate_message(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=waking())
    from dispatcher import messages
    messages.append(c.state_dir, "portfolio_eval", 42, "original wake", "op")
    intents_mod.write_intent(
        c.state_dir, "resume", "portfolio_eval", 42,
        {"model": "anthropic/claude-sonnet-5", "bypass_usage": True}, "op", 1)
    sess = FakeSessions()

    main.run_pass(c, deps(sess=sess))

    assert [(issue, model) for issue, _text, model, _effort in sess.resumed] == [
        (42, "anthropic/claude-sonnet-5")]
    assert "original wake" in sess.resumed[0][1]
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "original wake"]


def test_failed_intent_does_not_abort_pass_or_remaining_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 2)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, status=human(), park_msg_id=56)
    make_task(c, issue=44)  # active; keeps 42/43 waking... capacity 2 → one resumes

    real_apply = main._apply_operator_intent

    def apply_or_boom(cfg_, deps_, by_name, task, intent):
        if task.issue == 42:
            raise RuntimeError("disk full")
        real_apply(cfg_, deps_, by_name, task, intent)

    monkeypatch.setattr(main, "_apply_operator_intent", apply_or_boom)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 43, {}, "op", 2)
    main.run_pass(c, deps(sess=FakeSessions(alive={44})))  # must not raise
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN  # failed intent: unchanged
    assert load(c.state_dir, "portfolio_eval", 43).status.kind in (
        Kind.WAKING, Kind.RUNNING)  # later intent applied
    assert intents_mod.list_intents(c.state_dir) == []  # both files deleted


def test_dry_run_does_not_drain_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions()), dry_run=True)
    assert len(intents_mod.list_intents(c.state_dir)) == 1  # untouched


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


def test_reply_to_human_park_still_wakes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=human(), park_msg_id=88)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=88, text="hi")])
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert sess.sent_text == []
    # _wake marks PARK_WAKE and queues the text; _drive_wakes delivers it into the prompt.
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42)[0].text == "hi"
    assert sess.resumed and "hi" in sess.resumed[0][1]


def test_unmatched_reply_still_reports(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, status=login(), park_msg_id=77)
    monkeypatch.setattr(main.inbound, "fetch_events",
                        lambda sd: [main.Reply(reply_to_msg_id=999, text="x")])
    d = deps()
    main.run_pass(c, d)
    assert "status" in d.notifier.sent
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.HELD_LOGIN  # untouched


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


def test_reply_to_the_plan_parked_message_wakes_the_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)  # active task consumes the only capacity unit
    patch_events(monkeypatch, [Reply(reply_to_msg_id=55,
                                     text="drop the caching section")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "drop the caching section"


def test_plain_text_wakes_a_single_gate_parked_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)
    patch_events(monkeypatch, [Plain(text="ok")])
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "ok"


def test_plain_text_asks_which_when_a_human_park_and_a_gate_park_coexist(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    assert load(c.state_dir, "portfolio_eval", 43).status.kind is Kind.PARKED_REVIEW
    assert "status" in d.notifier.sent  # the "Which task?" prompt lists both


def test_which_task_prompt_names_the_project_when_multi_repository(
        tmp_path, monkeypatch):
    from tests.test_multi_project_capacity import two_repository_cfg
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=56)
    patch_events(monkeypatch, [Plain(text="yes")])
    d = deps()
    main.run_pass(c, d)
    prompt = "\n".join(line for t, ctx in d.notifier.calls if t == "status"
                       for line in ctx["lines"])
    assert "portfolio_eval#42" in prompt and "portfolio_eval#43" in prompt


def test_status_line_says_no_slot_for_a_gate_parked_task(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(
        issue=42, repository="portfolio_eval", stage=Stage.PLAN, status=review(),
        slot=NO_SLOT, worktree="/wt", branch="agent/task-42", title="Add widget",
        updated_at="2026-07-28T00:00:00+00:00",
        effort=1, labels=("auto",)))
    lines = main._status_lines(c)
    assert lines[0].endswith("[parked-review] (no slot)")
    assert "(slot -1)" not in lines[0]
    assert lines[-1] == "capacity 0/3"   # gate-parked holds no capacity


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


def test_prune_snapshots_removes_only_old_finished(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # old + finished (PR_OPEN is not in IN_FLIGHT_STAGES) -> pruned
    make_task(c, issue=41, stage=Stage.PR_OPEN)
    (snaps / "task-41.txt").write_text("done long ago")
    os.utime(snaps / "task-41.txt", (old, old))
    # old + absent state file -> pruned
    (snaps / "task-40.txt").write_text("state gone")
    os.utime(snaps / "task-40.txt", (old, old))
    # old + still in flight (parked implement) -> kept
    make_task(c, issue=42, status=human())
    (snaps / "task-42.txt").write_text("still parked")
    os.utime(snaps / "task-42.txt", (old, old))
    # fresh + finished -> kept (within the 7-day window)
    make_task(c, issue=43, stage=Stage.QUEUED, status=failed("unknown"))
    (snaps / "task-43.txt").write_text("just failed")

    main._prune_snapshots(c)

    assert not (snaps / "task-41.txt").exists()
    assert not (snaps / "task-40.txt").exists()
    assert (snaps / "task-42.txt").exists()
    assert (snaps / "task-43.txt").exists()


def test_prune_snapshots_corrupt_state_file_does_not_abort_sweep(tmp_path, monkeypatch):
    """A corrupt state file must not raise out of _prune_snapshots; the sweep
    continues and still prunes any other eligible snapshots."""
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # corrupt state file — write invalid JSON directly
    Path(c.state_dir, "task-50.json").write_text("{not json")
    (snaps / "task-50.txt").write_text("corrupt task snap")
    os.utime(snaps / "task-50.txt", (old, old))
    # separate old+finished snapshot in the same dir (absent state file) -> must still be pruned
    (snaps / "task-51.txt").write_text("old finished")
    os.utime(snaps / "task-51.txt", (old, old))

    # must not raise
    main._prune_snapshots(c)

    # sweep continued past the corrupt file and pruned the eligible one
    assert not (snaps / "task-51.txt").exists()


def test_prune_snapshots_skips_the_sweep_when_state_cannot_be_read(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    (snaps / "task-52.txt").write_text("maybe still in flight")
    os.utime(snaps / "task-52.txt", (old, old))

    def unreadable(_state_dir):
        raise OSError("state dir unreadable")
    monkeypatch.setattr(main, "load_all", unreadable)

    main._prune_snapshots(c)

    assert (snaps / "task-52.txt").exists()


# ---------------------------------------------------------------------------
# PR lifecycle tests (Task 8)
# ---------------------------------------------------------------------------


def test_merged_pr_moves_task_to_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess=sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.status.kind is Kind.DONE and got.done_at
    assert (42, "Done") in gh.statused
    assert 42 in sess.ended
    assert removed == [(t.worktree, t.branch, False)]
    assert (c.repositories[0].repo, t.branch) in gh.deleted_branches
    assert "task_done" in notif.sent


def test_merged_on_a_board_without_done_finishes_the_task_with_a_warning(
        tmp_path, monkeypatch, capsys):
    """No Done option is the old empty-id opt-out: the board is skipped,
    loudly, and the merged task still finishes (a retry could never pass)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class NoDone(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise BoardError("project jesdi/1: field 'Status' has no option 'Done'")

    gh = NoDone()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))  # must not raise
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE
    assert len(removed) == 1
    assert "has no option 'Done'" in capsys.readouterr().err


def test_closed_unmerged_pr_fails_task_preserving_worktree(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    notif = FakeNotifier()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert removed == [] and "pr_closed" in notif.sent
    assert sess.ended == [42]   # the close ends whatever session is left


def test_feedback_sets_pending_flag_and_notifies_once(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)  # full: spawn deferred
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert feedback_pending(got) is True and got.stage is Stage.PR_OPEN
    assert got.feedback_cursor == ""  # cursor NOT advanced here; that is Task 9's job
    assert notif.sent.count("pr_feedback") == 1
    main.run_pass(c, deps(gh, notifier=notif))   # second pass: no re-notify
    assert notif.sent.count("pr_feedback") == 1


def test_own_and_bot_comments_do_not_reopen(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[
        {"createdAt": "2026-07-30T09:00:00Z", "author": {"login": "agent-bot"}},
        {"createdAt": "2026-07-30T09:01:00Z",
         "author": {"login": "github-actions[bot]"}}])
    main.run_pass(c, deps(gh))
    assert feedback_pending(load(c.state_dir, "portfolio_eval", 42)) is False


def test_pr_number_resolved_from_branch_when_missing(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, pr_number=0)
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, "portfolio_eval", 42).pr_number == 12


def test_poll_failure_leaves_task_untouched(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_view_raises = True
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN and feedback_pending(got) is False


def test_merge_board_failure_leaves_task_pr_open_and_pass_survives(
        tmp_path, monkeypatch):
    """set_status raising mid-FinishMerged must be caught per-task: the pass
    continues (no exception escapes run_pass), the task stays PR_OPEN (state
    flip never reached), and the worktree was NOT removed (teardown runs after
    the raising step so it too was never reached)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class SetStatusFails(FakeGitHub):
        def set_status(self, repository, issue, status):
            raise subprocess.CalledProcessError(1, ["gh"])

    gh = SetStatusFails()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))  # must not raise
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN  # state flip never reached
    assert removed == []               # teardown never reached (board runs first)


def test_closed_pr_on_a_running_address_review_ends_its_session_failed(
        tmp_path, monkeypatch):
    """The close ends the live address-review pane and frees its slot."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.ADDRESS_REVIEW, status=RUNNING, slot=0, pr_number=12)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    notif, sess = FakeNotifier(), FakeSessions(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status, got.slot) == (
        Stage.ADDRESS_REVIEW, failed("pr-closed"), NO_SLOT)
    assert 42 in sess.ended and notif.sent.count("pr_closed") == 1


def test_closed_pr_on_a_waking_task_ends_it_failed(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)     # the wake cannot launch
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status) == (Stage.PR_OPEN, failed("pr-closed"))
    assert sess.spawned == []


def test_merge_whose_teardown_fails_halfway_stays_parked_and_retries(
        tmp_path, monkeypatch):
    """The teardown runs before the commit: a raise leaves parked(pr), and
    the PR, still merged, finishes the task next pass."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)

    class DeleteFailsOnce(FakeGitHub):
        def delete_branch(self, repository, branch):
            if not self.deleted_branches:
                self.deleted_branches.append(None)
                raise subprocess.CalledProcessError(1, ["git"])
            super().delete_branch(repository, branch)

    gh = DeleteFailsOnce()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))  # must not raise
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.status, got.done_at, len(removed)) == (PR, "", 1)
    assert "task_done" not in notif.sent and _events(c, "merged") == []
    main.run_pass(c, deps(gh, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.status == DONE and got.done_at
    assert notif.sent.count("task_done") == 1 and len(_events(c, "merged")) == 1


def test_merge_after_an_exhausted_park_ends_done(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=human("loop-cap", "ci loop hit its cap"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.stage, got.status) == (Stage.PR_OPEN, DONE) and got.done_at


class _RealPanes(FakeSessions):
    """Panes as podman keeps them: ending one kills it, and a launch beside a
    live one is refused (sessions._free_name)."""

    def end(self, repository, issue):
        super().end(repository, issue)
        self.alive_set.discard(issue)

    def spawn_stage(self, repository, issue, *a, **kw):
        if issue in self.alive_set:
            raise RuntimeError(f"container for #{issue} is still running")
        super().spawn_stage(repository, issue, *a, **kw)
        self.alive_set.add(issue)


def test_merge_during_address_review_whose_teardown_fails_finishes_next_pass(
        tmp_path, monkeypatch):
    """The teardown runs before the pane is ended: a raise leaves the
    address-review session alive, so the next pass finds no crash and the
    still-merged PR finishes the task."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.ADDRESS_REVIEW, status=RUNNING, slot=0, pr_number=12)

    class CloseFailsOnce(FakeGitHub):
        failed = False

        def close_issue(self, repo, issue):
            if not self.failed:
                self.failed = True
                raise subprocess.CalledProcessError(1, ["gh"])
            super().close_issue(repo, issue)

    gh = CloseFailsOnce()
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-07-30T10:00:00Z")
    notif, sess = FakeNotifier(), _RealPanes(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    main.run_pass(c, deps(gh, sess, notifier=notif))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert (got.status, got.slot) == (DONE, NO_SLOT) and got.done_at
    assert len(removed) == 1 and sess.ended == [42]
    assert "session_crashed" not in notif.sent and notif.sent.count("task_done") == 1


def test_feedback_on_a_parked_pr_with_a_live_pane_ends_it_before_address_review(
        tmp_path, monkeypatch):
    """A task parked at pr-open before the pr-open transition ended its pane
    still has a live review session: the address-review launch ends it."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=PR)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    notif, sess = FakeNotifier(), _RealPanes(alive={42})
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.ADDRESS_REVIEW, RUNNING)
    assert sess.ended == [42] and sess.spawned[-1][1] == "address-review"


def test_conflicts_past_the_cap_park_the_pr_open_task_on_a_human(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=1))
    pr_open_task(c, ci_rounds=1)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    notif, sess = FakeNotifier(), FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.conflict_cursor) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, "abc")
    assert t.status.reason is HumanReason.LOOP_CAP
    assert "parked_question" in notif.sent and not sess.spawned


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


def test_claims_stop_at_max_open_even_with_capacity_free(tmp_path, monkeypatch):
    """Parked and pr-open tasks hold no capacity but still hold a
    worktree; max_open counts them so the box stops claiming."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), max_open=2)
    make_task(c, issue=1, stage=Stage.PLAN, status=review(), slot=NO_SLOT)
    pr_open_task(c, issue=2)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert sess.spawned == []


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

def merged_cfg(tmp_path):
    return cfg(tmp_path)


def test_dry_run_pass_does_not_tear_down_a_merged_task(tmp_path, monkeypatch):
    """--dry-run must reach remove_workspace as a dry run, not as a real
    deletion: GitHubClient and Sessions already no-op under dry_run, so the
    worktree/branch removal was the one side effect that still executed for
    real while the operator watched `[dry-run] delete_branch …` scroll by."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    t = pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    assert removed == [(t.worktree, t.branch, True)]


def test_remove_workspace_dry_run_flag_reaches_the_real_teardown(
        tmp_path, monkeypatch, capsys):
    """Same path with the real workspace module in place: nothing shells
    out. Guards the signature itself — a remove_workspace that ignored
    dry_run would still run git here."""
    patch_usage(monkeypatch)
    from dispatcher import workspace as workspace_mod
    monkeypatch.setattr(
        workspace_mod, "_sh",
        lambda *a, **k: pytest.fail("git ran under --dry-run"))
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)
    # The merged branch really was taken (so _sh had its chance to run),
    # and the task stayed put — a dry run reports the teardown, never
    # performs it and never records it as performed.
    assert "[dry-run] remove worktree" in capsys.readouterr().out
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN


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


def test_spawned_cursor_still_sees_a_comment_from_the_spawn_second(
        tmp_path, monkeypatch):
    """End to end: spawn address-review, then post a comment stamped with
    the whole second the spawn happened in. The next poll must classify it
    as fresh feedback (a redundant round beats a lost comment)."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    main.run_pass(c, deps(gh))
    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.ADDRESS_REVIEW and got.feedback_cursor

    # Derive the spawn second from the cursor the pass actually wrote.
    # _cursor_now() returns floor(now) - 1s, so the second a comment posted
    # during the spawn can carry is cursor + 1s.  Sampling the clock before
    # run_pass is racy: if the wall-clock second ticks over between the sample
    # and the _cursor_now() call, the derived stamp can be one second off.
    #
    # Non-tautology guard: assert spawn_second is not in the future.
    # If _cursor_now() stops subtracting, cursor = floor(now), making
    # spawn_second = floor(now) + 1s — a future second — and this assert fires.
    # (run_pass completes in <1 ms in tests; a 1-second gap is impossible.)
    cursor_dt = datetime.fromisoformat(got.feedback_cursor)
    spawn_second = cursor_dt + timedelta(seconds=1)
    assert spawn_second <= datetime.now(timezone.utc).replace(microsecond=0), (
        f"feedback_cursor {got.feedback_cursor!r} looks like floor(now), not "
        f"floor(now)-1s — _cursor_now() may have stopped subtracting"
    )
    stamp = spawn_second.strftime("%Y-%m-%dT%H:%M:%SZ")
    res = main.pr_poll.classify(
        payload(comments=[{"createdAt": stamp,
                           "author": {"login": "alice"}}]),
        got.feedback_cursor, "agent-bot")
    assert res.kind == "feedback"


def test_address_review_done_returns_task_to_pr_open(tmp_path, monkeypatch):
    """The return leg, at run_pass level: a task that STARTS the pass at
    address-review with a `done` signal must land back at pr-open with its
    PR number intact, and _poll_prs — which re-reads it later in the same
    pass, now against the freshly-advanced cursor — must not re-flag
    feedback for a comment older than that cursor or spawn a second round."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    cursor = "2026-07-30T09:30:00+00:00"
    wt = make_task(c, issue=42, stage=Stage.ADDRESS_REVIEW, slot=0,
                   pr_number=12, feedback_cursor=cursor)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "address-review", "status": "done",
        "note": "pushed review fixes", "artifact": ""}))
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{
        "createdAt": "2026-07-30T09:00:00Z", "author": {"login": "alice"}}])
    sess = FakeSessions(alive=(42,))
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, sess, notifier=notif))

    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN
    assert got.pr_number == 12              # regex found nothing; not clobbered
    assert feedback_pending(got) is False    # comment predates the cursor
    assert got.feedback_cursor == cursor    # only a spawn moves it
    assert sess.spawned == []               # no second address-review round
    assert "pr_updated" in notif.sent


# ---------------------------------------------------------------------------
# --dry-run must not touch local state
#
# Every I/O *dependency* was already stubbed under --dry-run (GitHubClient,
# Sessions, Notifier, remove_workspace), but the local writes — state files,
# events.jsonl, and the markers/caches that live beside them — ran for real.
# A dry run therefore advanced tasks to a terminal stage and wrote history
# while every visible side effect printed "[dry-run] …" and was skipped.
# ---------------------------------------------------------------------------

def test_dry_run_over_a_merged_pr_leaves_state_and_events_untouched(
        tmp_path, monkeypatch):
    """The reproduced production bug: a dry run over four merged PRs flipped
    them pr-open → done and appended four `merged` events, so the real pass
    that follows never performs the teardown (_poll_prs only sees PR_OPEN)."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c, pr_number=0)          # forces the pr_number backfill too
    gh = FakeGitHub()
    gh.branch_prs["agent/task-42"] = 12
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")
    main.run_pass(c, deps(gh), dry_run=True)

    got = load(c.state_dir, "portfolio_eval", 42)
    assert got.stage is Stage.PR_OPEN
    assert got.done_at == ""
    assert got.pr_number == 0             # not even the benign backfill
    assert eventlog.read_tail(c.state_dir) == []


def test_dry_run_does_not_write_state_for_a_newly_claimed_task(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    gh = FakeGitHub([Candidate(42, "Add widget", "u42")])
    main.run_pass(c, deps(gh), dry_run=True)

    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert eventlog.read_tail(c.state_dir) == []


def test_dry_run_leaves_the_real_state_dir_alone_entirely(
        tmp_path, monkeypatch):
    """Belt-and-braces over the whole directory: no file under state_dir may
    change during a dry run, whatever the pass decides to do."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = merged_cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="MERGED",
                                 merged_at="2026-07-30T10:00:00Z")

    def snapshot():
        root = Path(c.state_dir)
        return {p.relative_to(root): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()
                and p.name != "convergence.lock"}

    before = snapshot()
    main.run_pass(c, deps(gh), dry_run=True)
    assert snapshot() == before


# ---------------------------------------------------------------------------
# auth-dark edge tests (Task 3)
# ---------------------------------------------------------------------------

def patch_usage_dark(monkeypatch):
    monkeypatch.setattr(main, "fetch_all",
                        lambda cfg, **k: {"anthropic": session_usage(0.0, 0.0, source="unavailable")})


def dark_marker(c):
    return Path(c.state_dir) / "auth-dark"


def test_auth_dark_first_pass_marks_but_does_not_alert(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_silent_during_grace_and_since_survives(tmp_path, monkeypatch):
    """Second pass inside the grace window must not alert and must not
    rewrite 'since' — exercises the _now()->fromisoformat round-trip."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                        # pass 1: creates marker
    since_1 = json.loads(dark_marker(c).read_text())["since"]
    main.run_pass(c, d)                        # pass 2: still within grace
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert st["since"] == since_1              # since not rewritten
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_alerts_once_after_grace(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                       # creates the marker
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=main.AUTH_DARK_GRACE_MINUTES + 1)).isoformat()
    dark_marker(c).write_text(json.dumps({"since": old, "alerted": False}))
    main.run_pass(c, d)                       # past grace -> alert
    main.run_pass(c, d)                       # already alerted -> silent
    assert d.notifier.sent.count("auth_dark") == 1
    assert json.loads(dark_marker(c).read_text())["alerted"] is True


def test_auth_dark_clears_on_recovery(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    patch_usage_dark(monkeypatch)
    main.run_pass(c, d)
    assert dark_marker(c).exists()
    patch_usage(monkeypatch, util=0.2)        # usage readable again
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_budget_full_with_live_source_never_touches_auth_dark(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)       # full window, oauth source
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_unavailable_usage_sends_no_budget_stall(tmp_path, monkeypatch):
    """An auth outage fail-safes to no-spawns, but it is not a budget stall:
    "usage window exhausted; stalled until reset" would send the operator to
    wait out an outage only a re-login ends, 30 minutes before the accurate
    auth_dark alert. auth-dark owns the unavailable case."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert "budget_stall" not in d.notifier.sent
    assert not (Path(c.state_dir) / "budget-stalled").exists()


def test_full_window_with_readable_usage_still_sends_budget_stall(
        tmp_path, monkeypatch):
    """Guard on the other side of the suppression: a genuinely exhausted
    window with a live source keeps its stall ping (and its resume)."""
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_stall") == 1
    assert (Path(c.state_dir) / "budget-stalled").exists()
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1


def test_auth_dark_corrupt_marker_is_repaired(tmp_path, monkeypatch):
    """Non-JSON marker must not crash the pass; it is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text("not valid json{{")
    main.run_pass(c, d)                        # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_malformed_marker_is_repaired(tmp_path, monkeypatch):
    """Parseable-but-malformed JSON (null, missing 'since') must not crash the
    pass; the marker is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text(json.dumps(None))   # valid JSON, non-dict
    main.run_pass(c, d)                            # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _realert_h(**extra):
    return timedelta(hours=main.AUTH_DARK_REALERT_HOURS, **extra)


def _dark_pass(tmp_path, monkeypatch, marker=None):
    """Dark pass(es) setup: returns (cfg, deps) with `marker` (dict or raw
    str) written to the auth-dark file when given."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    if marker is not None:
        Path(c.state_dir).mkdir(parents=True, exist_ok=True)
        dark_marker(c).write_text(
            marker if isinstance(marker, str) else json.dumps(marker))
    return c, d


def _marker(c):
    return json.loads(dark_marker(c).read_text())


def test_auth_dark_realert_hours_is_eight():
    assert main.AUTH_DARK_REALERT_HOURS == 8


def test_auth_dark_silent_at_29_minutes(tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch,
                      {"since": _ago(minutes=29), "alerted": False})
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c)["alerted"] is False


def test_auth_dark_stray_last_alert_does_not_skip_grace(tmp_path, monkeypatch):
    m = {"since": _ago(minutes=10), "alerted": False, "last_alert": "garbage"}
    c, d = _dark_pass(tmp_path, monkeypatch, m)
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c) == m


def test_auth_dark_first_alert_records_last_alert(tmp_path, monkeypatch):
    since = _ago(minutes=main.AUTH_DARK_GRACE_MINUTES + 1)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": False})
    main.run_pass(c, d)
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since and st["alerted"] is True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_realerts_after_interval(tmp_path, monkeypatch):
    since = _ago(hours=9)
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": since, "alerted": True,
        "last_alert": (datetime.now(timezone.utc) - _realert_h(minutes=1)).isoformat()})
    main.run_pass(c, d)
    main.run_pass(c, d)                        # back to back: silent
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since and st["alerted"] is True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_realerts_exactly_at_interval(tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(hours=9), "alerted": True,
        "last_alert": (datetime.now(timezone.utc) - _realert_h()).isoformat()})
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1


def test_auth_dark_silent_just_inside_interval(tmp_path, monkeypatch):
    m = {"since": (datetime.now(timezone.utc) - _realert_h()).isoformat(),
         "alerted": True,
         "last_alert": (datetime.now(timezone.utc) - _realert_h()
                        + timedelta(minutes=1)).isoformat()}
    c, d = _dark_pass(tmp_path, monkeypatch, m)
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c) == m


def test_auth_dark_long_outage_keeps_reminding(tmp_path, monkeypatch):
    def stale():
        return (datetime.now(timezone.utc) - _realert_h(minutes=1)).isoformat()
    c, d = _dark_pass(tmp_path, monkeypatch,
                      {"since": _ago(hours=21), "alerted": True,
                       "last_alert": stale()})
    main.run_pass(c, d)
    st = _marker(c)
    st["last_alert"] = stale()
    dark_marker(c).write_text(json.dumps(st))
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 2


def test_auth_dark_realert_reports_minutes_since_incident_began(
        tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(minutes=540), "alerted": True,
        "last_alert": _ago(minutes=481)})
    main.run_pass(c, d)
    sent = [ctx for t, ctx in d.notifier.calls if t == "auth_dark"]
    assert len(sent) == 1
    assert abs(sent[0]["minutes"] - 540) <= 1
    assert sent[0]["host"] == socket.gethostname()


def test_auth_dark_new_incident_after_recovery_starts_with_grace(
        tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(hours=2), "alerted": True, "last_alert": _ago(hours=1)})
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent
    patch_usage_dark(monkeypatch)
    main.run_pass(c, d)
    st = _marker(c)
    assert st["alerted"] is False and "last_alert" not in st
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_legacy_marker_realerts_once_past_interval(
        tmp_path, monkeypatch):
    since = _ago(hours=9)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    main.run_pass(c, d)
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_legacy_marker_realerts_exactly_at_interval(
        tmp_path, monkeypatch):
    since = (datetime.now(timezone.utc) - _realert_h()).isoformat()
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1


def test_auth_dark_legacy_marker_inside_interval_stays_silent(
        tmp_path, monkeypatch):
    since = _ago(hours=2)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c)["since"] == since


@pytest.mark.parametrize("raw", [
    "not valid json{{",
    "null",
    json.dumps({"alerted": True}),
    json.dumps({"since": "garbage", "alerted": True,
                "last_alert": _ago(hours=9)}),
    json.dumps({"since": _ago(hours=9), "alerted": True,
                "last_alert": "garbage"}),
], ids=["non-json", "null", "no-since", "bad-since", "bad-last-alert"])
def test_auth_dark_corrupt_marker_resets_without_alert(
        tmp_path, monkeypatch, raw):
    c, d = _dark_pass(tmp_path, monkeypatch, raw)
    main.run_pass(c, d)                        # must not raise
    st = _marker(c)
    assert st["alerted"] is False and "last_alert" not in st
    assert datetime.fromisoformat(st["since"]) > (
        datetime.now(timezone.utc) - timedelta(minutes=1))
    assert "auth_dark" not in d.notifier.sent


def _fail_then_recover(tmp_path, monkeypatch, marker):
    c, d = _dark_pass(tmp_path, monkeypatch, marker)
    before = _marker(c)
    d.notifier.msg_id = 0
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1
    assert _marker(c) == before
    d.notifier.msg_id = 77
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 2
    assert _marker(c)["last_alert"] != before.get("last_alert")


def test_failed_reminder_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(hours=9), "alerted": True,
        "last_alert": _ago(seconds=_realert_h(minutes=1).total_seconds())})


def test_failed_first_alert_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(minutes=main.AUTH_DARK_GRACE_MINUTES + 1),
        "alerted": False})

def test_pass_writes_heartbeat(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    d = deps()
    main.run_pass(c, d)
    hb = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert hb["interval_minutes"] == c.pass_interval_minutes
    assert hb["started_at"] <= hb["finished_at"]


def test_reply_to_a_running_task_is_queued_not_dropped(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=RUNNING)           # live, unparked
    main.intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    queued = messages.undelivered(c.state_dir, "portfolio_eval", 42)
    assert [m.text for m in queued] == ["use oauth"]
    assert [m.actor for m in queued] == ["jesdi@github"]
    # a running task is NOT woken — mail waits for the next boundary
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING


def test_reply_to_a_wake_pending_task_is_queued_and_not_clobbered(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=waking())
    for i, text in enumerate(["first", "second"], start=1):
        main.intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": text},
                                  "jesdi@github", i)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "first", "second"]


def test_reply_to_an_unclaimed_issue_is_held(tmp_path):
    c = cfg(tmp_path)
    main.intents.write_intent(c.state_dir, "reply", "portfolio_eval", 777,
                              {"text": "pre-brief: use the v2 API"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 777)] == [
        "pre-brief: use the v2 API"]


def test_reply_to_a_done_task_is_held(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, status=DONE)
    main.intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "one more"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert len(messages.undelivered(c.state_dir, "portfolio_eval", 42)) == 1


def test_reply_to_a_parked_task_queues_and_requests_a_wake(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human())
    main.intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "use oauth"]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_resume_intent_queues_no_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human())
    main.intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "jesdi@github", 1)
    main._apply_intents(c, deps())
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_ci_conclusion_becomes_a_dispatcher_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=ci(4242))
    main._wake_ci(c, deps(gh=FakeGitHub(run_conclusion="failure")),
                  c.repositories[0])
    from dispatcher import messages
    queued = messages.undelivered(c.state_dir, "portfolio_eval", 42)
    assert len(queued) == 1
    assert "4242 concluded: failure" in queued[0].text
    assert queued[0].actor == "dispatcher"
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.status.run_id == 0


def test_finished_ci_run_wakes_the_task_through_the_table(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="success")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.status.cause is Cause.CI
    from dispatcher import messages
    assert ["991 concluded: success" in m.text
            for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [True]


def test_failed_ci_run_past_the_cap_parks_for_the_operator(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT, e2e_rounds=3)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="failure")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.status.reason) == (Kind.PARKED_HUMAN, HumanReason.LOOP_CAP)
    assert t.e2e_rounds == 4
    assert [e["detail"] for e in events(c, "round")] == ["e2e round 4/3: run 991 failure"]
    assert t.status.note == "e2e loop exceeded its cap of 3 rounds (run 991 failure)"


def test_ci_run_still_in_progress_leaves_the_task_parked(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), capacity=0)
    make_task(c, issue=42, status=ci(991), slot=NO_SLOT)
    main.run_pass(c, deps(gh=FakeGitHub(run_conclusion="")))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == ci(991)
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


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


def test_reconcile_keeps_the_marker_of_a_task_that_is_still_starving(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    make_task(c, issue=43, slot=NO_SLOT, stage=Stage.PR_OPEN, status=waking(PR, "attention(feedback)"))
    state._wake_blocked_path(c, "portfolio_eval", 42).touch()
    state._wake_blocked_path(c, "portfolio_eval", 43).touch()
    main._reconcile_wake_blocked(c)
    assert state._wake_blocked_path(c, "portfolio_eval", 42).exists()   # wake still denied
    assert state._wake_blocked_path(c, "portfolio_eval", 43).exists()   # feedback spawn still denied


def test_reconcile_drops_the_marker_of_a_parked_task_with_pending_feedback(tmp_path):
    """A parked task is not spawned for its feedback (a wake starts the
    round): it waits for nothing."""
    c = cfg(tmp_path)
    make_task(c, issue=43, slot=NO_SLOT, stage=Stage.PR_OPEN, status=human())
    state._wake_blocked_path(c, "portfolio_eval", 43).parent.mkdir(parents=True, exist_ok=True)
    state._wake_blocked_path(c, "portfolio_eval", 43).touch()
    main._reconcile_wake_blocked(c)
    assert not state._wake_blocked_path(c, "portfolio_eval", 43).exists()


def test_reconcile_drops_a_marker_left_by_a_vanished_task(tmp_path):
    c = cfg(tmp_path)
    state._wake_blocked_path(c, "portfolio_eval", 42).parent.mkdir(parents=True, exist_ok=True)
    state._wake_blocked_path(c, "portfolio_eval", 42).touch()      # state file already flushed
    main._reconcile_wake_blocked(c)
    assert not state._wake_blocked_path(c, "portfolio_eval", 42).exists()


def test_legacy_wake_blocked_marker_is_rekeyed_with_one_repository(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    legacy = Path(c.state_dir) / "wake-blocked-42"
    legacy.touch()
    main._migrate_legacy_keys(c)
    assert not legacy.exists()
    assert state._wake_blocked_path(c, "portfolio_eval", 42).exists()


def test_legacy_wake_blocked_marker_is_dropped_with_several_repositories(tmp_path):
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0],
                                    dc_replace(c.repositories[0], name="factorial")])
    legacy = Path(c.state_dir) / "wake-blocked-42"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.touch()
    main._migrate_legacy_keys(c)
    assert not legacy.exists()
    assert not state._wake_blocked_path(c, "portfolio_eval", 42).exists()


def test_console_reply_to_a_human_park_still_queues(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)                      # holds the only capacity unit
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                             "jesdi@github", 1)
    sess = FakeSessions(alive={43})
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    assert sess.sent_text == []
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "use oauth"]


# --- CLI wiring: --migrate-tmux (delete with dispatcher/tmux_migration.py) ---

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


def test_failed_e2e_runs_count_and_park_past_the_cap(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(e2e=1))
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "review"))
    make_task(c, stage=Stage.REVIEW, status=ci(7), slot=NO_SLOT)
    gh = FakeGitHub(run_conclusion="failure"); d = deps(gh)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    # Round 1 of 1: counted, last-round ping, woken AND resumed in the same pass.
    assert (t.status.kind, t.e2e_rounds) == (Kind.RUNNING, 1) and "last_round" in d.notifier.sent
    # Guard: last_round note must be suffix-free ("e2e round 1/1", not "e2e round 1/1: run 7 failure")
    last_round_note = next(ctx["note"] for tmpl, ctx in d.notifier.calls if tmpl == "last_round")
    assert last_round_note == "e2e round 1/1", f"last_round note regressed: {last_round_note!r}"
    assert d.sessions.resumed
    # Simulate the resumed session parking for CI again and failing again.
    save(c.state_dir, dc_replace(t, status=ci(8)))
    d = deps(FakeGitHub(run_conclusion="failure")); main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.e2e_rounds) == (Kind.PARKED_HUMAN, 2)
    assert "parked_question" in d.notifier.sent
    assert events(c, "parked")[-1]["detail"] == "e2e loop exceeded its cap of 1 rounds (run 8 failure)"


def test_operator_wake_resets_the_round_budget(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=human(), park_msg_id=77, slot=NO_SLOT,
              gate_rounds=3, e2e_rounds=1)
    d = deps(sess=FakeSessions())
    monkeypatch.setattr(main.inbound, "fetch_events", lambda state_dir: [
        Reply(reply_to_msg_id=77, text="use the mock, skip the real gateway")])
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.gate_rounds, t.e2e_rounds) == (0, 0)
    assert t.status.kind is Kind.RUNNING and d.sessions.resumed  # woken and resumed in the same pass


def test_red_check_queues_address_review_with_its_reason(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.ci_rounds, t.check_cursor) == (Stage.ADDRESS_REVIEW, 1, "2026-09-07T10:00:00Z")
    assert "pr_attention" in notif.sent
    assert "check-failed" in sess.spawned[-1][3]
    # Back at pr-open with the same red check: no second round.
    save(c.state_dir, dc_replace(t, stage=Stage.PR_OPEN, status=PR))
    d = deps(gh); main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.PR_OPEN and not d.sessions.spawned


def test_conflict_queues_address_review_once_per_head(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.conflict_cursor) == (Stage.ADDRESS_REVIEW, "abc")
    assert "Reason for this round: conflict" in sess.spawned[-1][3]


def test_a_crashed_address_review_round_respawns_with_its_reason(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="abc")
    main.run_pass(c, deps(gh, FakeSessions()))
    t = load(c.state_dir, "portfolio_eval", 42)
    save(c.state_dir, dc_replace(t, status=waking(failed("session-crash"), "resume")))
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    [(_, stage, _, prompt, _)] = sess.spawned
    assert stage == "address-review" and "Reason for this round: conflict" in prompt


def test_ci_rounds_past_the_cap_park_the_pr_open_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=1))
    pr_open_task(c, ci_rounds=1)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, feedback_pending(t)) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, False)
    assert t.check_cursor == "2026-09-07T10:00:00Z"  # cursor persisted on exhaustion
    assert "parked_question" in notif.sent and not sess.spawned
    # Still polled for merge while parked.
    gh.pr_payloads[12] = payload(state="MERGED", merged_at="2026-09-07T12:00:00Z")
    patch_teardown(monkeypatch)
    main.run_pass(c, deps(gh))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.DONE


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


def test_human_feedback_resets_ci_rounds(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, ci_rounds=2)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(comments=[{"createdAt": "2026-09-07T09:00:00Z",
                                           "author": {"login": "alice"}}])
    sess = FakeSessions(); main.run_pass(c, deps(gh, sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.ci_rounds == 0
    assert "Reason for this round: feedback" in sess.spawned[-1][3]


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


def test_a_reply_to_an_exhausted_pr_task_runs_address_review_once_admitted(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(replace_capacity(cfg(tmp_path), 1), loop_caps=LoopCaps(ci=3))
    pr_open_task(c, status=human("loop-cap"), ci_rounds=3)
    make_task(c, issue=43)                      # holds the only capacity unit
    sess = FakeSessions(alive={43})
    t = _reply_pass(c, "try once more", sess)
    assert (t.stage, t.status.kind, t.status.cause, t.ci_rounds) == (
        Stage.PR_OPEN, Kind.WAKING, Cause.REPLY, 0)
    delete(c.state_dir, "portfolio_eval", 43)
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status) == (Stage.ADDRESS_REVIEW, RUNNING)
    [(_, stage, _, prompt, _)] = sess.spawned
    assert stage == "address-review" and "try once more" in prompt


def test_ci_loop_exhausted_at_pr_open(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), loop_caps=LoopCaps(ci=3))
    pr_open_task(c, status=PR, ci_rounds=3)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload(head="abc")
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier(); sess = FakeSessions()
    main.run_pass(c, deps(gh, sess, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.status.reason, t.ci_rounds) == (
        Stage.PR_OPEN, Kind.PARKED_HUMAN, HumanReason.LOOP_CAP, 4)
    assert t.check_cursor == "2026-09-07T10:00:00Z"
    assert "parked_question" in notif.sent and not sess.spawned
    # A later conflict is ignored: the task waits for the operator.
    gh.pr_payloads[12] = payload(mergeable="CONFLICTING", head="def")
    main.run_pass(c, deps(gh, sess))
    assert load(c.state_dir, "portfolio_eval", 42) == t


def test_one_red_check_seen_across_two_passes_charges_one_round(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 0)   # the round is not launched
    pr_open_task(c, status=PR)
    gh = FakeGitHub(); gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-07T10:00:00Z")]
    notif = FakeNotifier()
    main.run_pass(c, deps(gh, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status, t.ci_rounds) == (waking(PR, "attention(check-failed)"), 1)
    save(c.state_dir, dc_replace(t, status=PR))   # back on GitHub, same red check
    main.run_pass(c, deps(gh, notifier=notif))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status, t.ci_rounds) == (PR, 1)
    assert notif.sent.count("pr_attention") == 1 and len(events(c, "round")) == 1


def test_console_reply_operator_wake_clears_all_four_counters(tmp_path, monkeypatch):
    from dispatcher import intents as intents_mod
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55,
              review_rounds=1, gate_rounds=2, e2e_rounds=1, ci_rounds=3)
    make_task(c, issue=43)  # holds the only slot → 42 stays waking
    intents_mod.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "try again"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0)


def test_successful_ci_wake_preserves_all_counters(tmp_path):
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT, status=ci(99),
              review_rounds=1, gate_rounds=1, e2e_rounds=2, ci_rounds=1)
    main._wake_ci(c, deps(gh=FakeGitHub(run_conclusion="success")), c.repositories[0])
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 1, 2, 1)


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

def test_failed_transition_clears_operator_request(tmp_path):
    """Slice 15a: kill intent → FAILED drops the request; spec_path preserved."""
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
              spec_path="docs/specs/design.md")
    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    main._apply_intents(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED, f"expected FAILED, got {t.stage}"
    assert operator_request(t) is None, (
        f"FAILED must clear operator_request, got {operator_request(t)!r}")
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


def test_canceled_transition_clears_operator_request(tmp_path):
    """Slice 15b: cancel intent → CANCELED drops the request; spec_path preserved."""
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.SPEC, status=answers(AnswersRequest(path=".agent/q.md")),
              spec_path="docs/specs/design.md")
    intents_mod.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    main._apply_intents(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.CANCELED, f"expected CANCELED, got {t.stage}"
    assert operator_request(t) is None, (
        f"CANCELED must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"
    assert t.slot == NO_SLOT
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

def test_pr_closed_failed_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15a-ii: PR closed unmerged (_poll_prs ~L762) clears operator_request."""
    patch_usage(monkeypatch)
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c, spec_path="docs/specs/design.md")
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(state="CLOSED")
    main.run_pass(c, deps(gh, FakeSessions(alive=(42,))))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED
    assert operator_request(t) is None, (
        f"pr-closed FAILED must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"


@pytest.mark.parametrize("state,expected", [("MERGED", Kind.DONE), ("CLOSED", Kind.FAILED)])
def test_terminal_pr_does_not_require_ci_access(tmp_path, monkeypatch, state, expected):
    import dispatcher.github as github
    patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] != ["gh", "pr", "view"] or "statusCheckRollup" in args[-1]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: CI denied")
        return json.dumps(payload(state=state, merged_at=(
            "2026-09-12T10:00:00Z" if state == "MERGED" else None)))

    monkeypatch.setattr(github, "_run", fake_run)
    client = github.GitHubClient()
    gh.pr_view = client.pr_view
    gh.ci_statuses = client.ci_statuses
    main._poll_prs(c, deps(gh), c.repositories[0])
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is expected
    assert len(calls) == 1  # terminal lifecycle never calls either CI endpoint


@pytest.mark.parametrize("feedback", [True, False])
def test_ci_denial_does_not_block_feedback_or_conflicts(tmp_path, monkeypatch, caplog, feedback):
    import dispatcher.github as github
    c = cfg(tmp_path)
    pr_open_task(c)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload(
        mergeable="CONFLICTING",
        comments=[{"createdAt": "2026-09-12T11:00:00Z",
                   "author": {"login": "reviewer"}}] if feedback else [])

    def denied(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: CI denied")

    monkeypatch.setattr(github, "_run", denied)
    gh.ci_statuses = github.GitHubClient().ci_statuses
    main._poll_prs(c, deps(gh), c.repositories[0])
    task = load(c.state_dir, "portfolio_eval", 42)
    assert feedback_pending(task)
    assert task.status.cause is (Cause.ATTENTION_FEEDBACK if feedback else Cause.ATTENTION_CONFLICT)
    if not feedback:
        assert "CI denied" in caplog.text
        assert task.check_cursor == ""


def test_parked_pr_does_not_poll_or_spend_ci_rounds(tmp_path):
    c = cfg(tmp_path)
    pr_open_task(c, status=human(), ci_rounds=2)
    gh = FakeGitHub()
    gh.pr_payloads[12] = payload()
    gh.ci = [CIStatus("failure", "2026-09-12T10:00:00Z")]
    main._poll_prs(c, deps(gh), c.repositories[0])
    assert gh.ci_calls == []
    assert load(c.state_dir, "portfolio_eval", 42).ci_rounds == 2


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
