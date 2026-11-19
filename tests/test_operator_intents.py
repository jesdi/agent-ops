"""Operator intents drained at the top of a pass: replies, answers, kill,
cancel, park, resume and retry, and how a refused or failed intent is recorded."""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher import eventlog, failures, intents, operator_intents
from dispatcher.deps import _now
from dispatcher.github import Candidate
from dispatcher.state import (NO_SLOT, AnswersRequest, LoopCaps, PlanApprovalRequest, Stage,
                              TaskState, delete, has_waiting, load, mark_waiting, operator_request,
                              save, SessionRecord, write_session)
from dispatcher.status import Cause, Kind
from tests.dispatchfakes import (FakeGitHub, FakeSessions, cfg, deps, make_task, patch_usage,
                                 patch_workspace, pr_open_task, replace_capacity, _events)
from tests.statuses import RUNNING, PR, CANCELED, DONE, human, answers, held, review, waking, failed


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
    intents.write_intent(c.state_dir, "kill", "agent_ops", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    operator_intents._apply_intents(c, deps(sess=sess))

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
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                             actor="jesdi@github", epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "use oauth"
    assert intents.list_intents(c.state_dir) == []
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
    intents.write_intent(c.state_dir, "reply", "", 42, {"text": "hi"},
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
    intents.write_intent(c.state_dir, "reply", "", 42, {"text": "hi"},
                             "op", 1)
    operator_intents._apply_intents(c, deps())
    mdir = Path(c.state_dir) / "messages"
    assert not mdir.exists() or list(mdir.iterdir()) == []
    assert "#42" in capsys.readouterr().err


def test_reply_intent_for_running_task_is_queued_but_task_stays_running(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)  # not parked — session is live
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "hi"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    # message is queued — not dropped — and the task stays unparked
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "hi"
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING  # no wake flip for a live session
    assert intents.list_intents(c.state_dir) == []  # intent deleted


def test_reply_intent_on_gate_parked_task_wakes_it(tmp_path, monkeypatch):
    # The console SpecPanel "approve" button sends a `reply` intent; the task
    # is parked for review (not for a question), so _apply_one_intent must accept it.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = replace_capacity(c, 1)
    make_task(c, issue=42, stage=Stage.PLAN, status=review(), slot=NO_SLOT, park_msg_id=55)
    make_task(c, issue=43)  # active task occupies the only slot → 42 stays waking
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "Approved — proceed."}, actor="jesdi@github",
                             epoch_ms=1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42)[0].text == "Approved — proceed."
    assert intents.list_intents(c.state_dir) == []


def _reply_pass(c, text, sess):
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": text},
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
    intents.write_intent(c.state_dir, "park", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == human("operator", "parked by operator")
    assert sess.ended == [42]
    assert "parked_question" not in d.notifier.sent   # the operator parked it: nothing to ask
    assert intents.list_intents(c.state_dir) == []


def test_kill_intent_ends_releases_and_tombstones_state(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    intents.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
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

    intents.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(ReleaseFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED  # tombstone written even when release raises


def test_kill_intent_does_not_reclaim_released_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
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
    intents.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    operator_intents._apply_intents(c, deps(sess=sess))
    assert sess.end_calls == [("portfolio_eval", 42)]
    assert load(c.state_dir, "portfolio_eval", 42) is None  # nothing to tombstone


def test_cancel_intent_ends_session_moves_board_and_tombstones(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    mark_waiting(c.state_dir, "portfolio_eval", 42)
    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
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

    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(CancelFailsGitHub(), sess))  # must not raise
    assert sess.ended == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED


def test_cancel_intent_does_not_reclaim_issue_same_pass(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42)
    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
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
    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.CANCELED
    assert t.slot == NO_SLOT


def test_cancel_intent_without_task_file_uses_intent_repository(tmp_path, monkeypatch):
    # A backlog card has no task file; the intent itself names the repository
    # so the board/issue side still happens. No tombstone is written.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 99,
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
    intents.write_intent(c.state_dir, "cancel", "", 99, {}, "op", 1)
    gh = FakeGitHub()
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert gh.canceled == []
    assert sess.end_calls == []


def _operator_intent_pass(c, action, sess=None, gh=None):
    intents.write_intent(c.state_dir, action, "portfolio_eval", 42, {}, "jesdi@github", 1)
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
    p = intents.write_intent(c.state_dir, "park", "portfolio_eval", 42, {}, "op", 1)
    p.write_text(json.dumps({**json.loads(p.read_text()), "action": "bogus"}))
    operator_intents._apply_intents(c, deps())
    assert "unknown intent action 'bogus'" in capsys.readouterr().err
    assert intents.list_intents(c.state_dir) == []


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
    intents.write_intent(c.state_dir, "retry", "portfolio_eval", 42, {}, "op", 1)
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
    intents.write_intent(c.state_dir, "retry", "portfolio_eval", 42, {}, "op", 1)
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
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
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
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {"text": "ship it"},
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
    intents.write_intent(c.state_dir, "resume", "", 42,
                             {"model": "openai/gpt-nope"}, "op", 1)
    operator_intents._apply_intents(c, deps())

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
    intents.write_intent(
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

    real_apply = operator_intents._apply_operator_intent

    def apply_or_boom(cfg_, deps_, by_name, task, intent):
        if task.issue == 42:
            raise RuntimeError("disk full")
        real_apply(cfg_, deps_, by_name, task, intent)

    monkeypatch.setattr(operator_intents, "_apply_operator_intent", apply_or_boom)
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 43, {}, "op", 2)
    main.run_pass(c, deps(sess=FakeSessions(alive={44})))  # must not raise
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN  # failed intent: unchanged
    assert load(c.state_dir, "portfolio_eval", 43).status.kind in (
        Kind.WAKING, Kind.RUNNING)  # later intent applied
    assert intents.list_intents(c.state_dir) == []  # both files deleted


def test_dry_run_does_not_drain_intents(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions()), dry_run=True)
    assert len(intents.list_intents(c.state_dir)) == 1  # untouched


def test_reply_to_a_running_task_is_queued_not_dropped(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=RUNNING)           # live, unparked
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    operator_intents._apply_intents(c, deps())
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
        intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": text},
                                  "jesdi@github", i)
    operator_intents._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "first", "second"]


def test_reply_to_an_unclaimed_issue_is_held(tmp_path):
    c = cfg(tmp_path)
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 777,
                              {"text": "pre-brief: use the v2 API"},
                              "jesdi@github", 1)
    operator_intents._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 777)] == [
        "pre-brief: use the v2 API"]


def test_reply_to_a_done_task_is_held(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.QUEUED, status=DONE)
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "one more"},
                              "jesdi@github", 1)
    operator_intents._apply_intents(c, deps())
    from dispatcher import messages
    assert len(messages.undelivered(c.state_dir, "portfolio_eval", 42)) == 1


def test_reply_to_a_parked_task_queues_and_requests_a_wake(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human())
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                              "jesdi@github", 1)
    operator_intents._apply_intents(c, deps())
    from dispatcher import messages
    assert [m.text for m in messages.undelivered(c.state_dir, "portfolio_eval", 42)] == [
        "use oauth"]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_resume_intent_queues_no_message(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, status=human())
    intents.write_intent(c.state_dir, "resume", "portfolio_eval", 42, {}, "jesdi@github", 1)
    operator_intents._apply_intents(c, deps())
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING


def test_console_reply_to_a_human_park_still_queues(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55)
    make_task(c, issue=43)                      # holds the only capacity unit
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42, {"text": "use oauth"},
                             "jesdi@github", 1)
    sess = FakeSessions(alive={43})
    main.run_pass(c, deps(sess=sess))
    from dispatcher import messages
    assert sess.sent_text == []
    assert [m.text for m in messages.all_messages(c.state_dir, "portfolio_eval", 42)] == [
        "use oauth"]


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


def test_console_reply_operator_wake_clears_all_four_counters(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    make_task(c, issue=42, status=human(), park_msg_id=55,
              review_rounds=1, gate_rounds=2, e2e_rounds=1, ci_rounds=3)
    make_task(c, issue=43)  # holds the only slot → 42 stays waking
    intents.write_intent(c.state_dir, "reply", "portfolio_eval", 42,
                             {"text": "try again"}, "op", 1)
    main.run_pass(c, deps(sess=FakeSessions(alive={43})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (0, 0, 0, 0)


def test_failed_transition_clears_operator_request(tmp_path):
    """Slice 15a: kill intent → FAILED drops the request; spec_path preserved."""
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
              spec_path="docs/specs/design.md")
    intents.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={42})))
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
    intents.write_intent(c.state_dir, "cancel", "portfolio_eval", 42, {}, "op", 1)
    operator_intents._apply_intents(c, deps(sess=FakeSessions(alive={42})))
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
