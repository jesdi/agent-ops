"""The executor: _apply/_Turn, the _ACT handlers, admissions, spec publish
at the gate, and crash reports."""
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import (eventlog, executor, intents as intents_mod, launch_choice, spec_publish,
                        operator_intents, stage_launch)
from dispatcher.state import (NO_SLOT, AnswersRequest, PlanApprovalRequest, SessionRecord, Stage,
                              TaskState, at_gate, load, operator_request, save, write_session)
from dispatcher.status import FailReason, Kind
from tests.dispatchfakes import (ADMIT_ALL, DENY_ALL, PLAN_SUMMARY,
                                 SPEC_PATH, FakeGitHub, FakeSessions,
                                 cfg, deps, make_task, patch_usage,
                                 patch_workspace, replace_capacity, SPEC_URL, _events, _plan_ready_task)
from tests.statuses import RUNNING, PR, human, answers, held, waking, failed


def test_every_action_a_session_event_emits_has_a_handler():
    """The executor dispatches on the action type; an action the table can
    emit for a session event without a handler would crash the task. The
    other event families wire theirs with their own tickets."""
    import typing
    from dispatcher import lifecycle as lc
    later = {lc.TypeLoginCode}
    assert set(typing.get_args(lc.Action)) - later == set(executor._ACT)


def test_awaiting_review_publishes_comments_and_links(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    _plan_ready_task(c)
    seen = []
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: (seen.append(kw)
                                      or spec_publish.PublishResult(url=SPEC_URL)))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)
    # backstop called with the task's real coordinates: the spec, whose
    # folder is published, never the summary under .agent/
    assert seen[0]["branch"] == "agent/task-42"
    assert seen[0]["repo"] == "jesdi/portfolio_eval"
    assert seen[0]["artifact"] == SPEC_PATH
    # one-tap link on the issue…
    assert gh.comments == [(42, f"📝 Plan ready for review: {SPEC_URL}")]
    # …and in the Telegram ping
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_plan_review"]
    assert f"spec: {SPEC_URL}" in ctx["note"]
    assert ctx["note"].startswith("plan ready")


def test_publish_failure_says_local_only_and_skips_comment(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    _plan_ready_task(c)
    monkeypatch.setattr(
        spec_publish, "ensure_published",
        lambda **kw: spec_publish.PublishResult(error="git push failed: auth"))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)
    assert gh.comments == []
    # the gate itself is NOT blocked by the failure
    assert at_gate(load(c.state_dir, "portfolio_eval", 42))
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_plan_review"]
    assert "⚠️ spec is local only: git push failed: auth" in ctx["note"]


def test_comment_failure_is_best_effort(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    _plan_ready_task(c)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url=SPEC_URL))
    gh = FakeGitHub(); gh.comment_raises = True
    d = deps(gh, FakeSessions(alive={42}))
    main.run_pass(c, d)  # must not raise
    assert at_gate(load(c.state_dir, "portfolio_eval", 42))
    assert any(t == "awaiting_plan_review" for t in d.notifier.sent)
    (tmpl, ctx), = [x for x in d.notifier.contexts
                    if x[0] == "awaiting_plan_review"]
    assert f"spec: {SPEC_URL}" in ctx["note"]


def test_gate_entry_arms_the_request_and_keeps_spec_path_and_track(tmp_path, monkeypatch):
    """Gate entry sets operator_request to the summary; spec_path (the
    folder's spec.md) and the track recorded at the spec handover stay."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    _plan_ready_task(c, track="deep")
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="https://example.com/spec"))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t)
    assert operator_request(t) == PlanApprovalRequest(PLAN_SUMMARY)
    assert t.spec_path == SPEC_PATH
    assert t.track == "deep"


@pytest.mark.parametrize("artifact", ["../outside.md", "/etc/passwd"])
def test_gate_bounces_an_unusable_artifact_instead_of_the_default_page(
        tmp_path, monkeypatch, artifact):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = _plan_ready_task(c)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "awaiting-review", "artifact": artifact}))
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url=SPEC_URL))
    main.run_pass(c, deps(sess=FakeSessions(alive={42})))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.stage is Stage.PLAN and operator_request(t) is None and t.plan_retries == 1


def test_repeated_awaiting_review_preserves_grace_and_request(tmp_path, monkeypatch):
    """Slice 8: a second awaiting-review signal while already at the gate must
    not restart the grace clock (updated_at unchanged) and must not overwrite
    operator_request or spec_path.  Machine returns NoOp on repeat → no save."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    _plan_ready_task(c)
    monkeypatch.setattr(spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="https://example.com/spec"))
    sess = FakeSessions(alive={42})
    d = deps(sess=sess)
    # First pass: plan running → held at the gate, its request armed, updated_at stamped.
    main.run_pass(c, d)
    t1 = load(c.state_dir, "portfolio_eval", 42)
    assert at_gate(t1)
    assert operator_request(t1) == PlanApprovalRequest(PLAN_SUMMARY)
    # Second pass: same signal, grace not elapsed → NoOp; nothing should change.
    main.run_pass(c, d)
    t2 = load(c.state_dir, "portfolio_eval", 42)
    assert t2.updated_at == t1.updated_at, "grace deadline must not be restarted"
    assert operator_request(t2) == PlanApprovalRequest(PLAN_SUMMARY), "operator_request must not change"
    assert t2.spec_path == SPEC_PATH, "spec_path must not change"
    assert d.notifier.sent.count("awaiting_plan_review") == 1


def test_gate_respawn_keeps_spec_path_and_track(tmp_path, monkeypatch):
    # Reboot recovery: gate-parked task with a dead session re-spawns PLAN,
    # with or without a stage.json left behind.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PLAN, status=held(PlanApprovalRequest(PLAN_SUMMARY)), spec_path=SPEC_PATH, track="deep")
    sess = FakeSessions()  # session not alive
    main.run_pass(c, deps(sess=sess))
    assert [(s[0], s[1]) for s in sess.spawned] == [(42, "plan")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.stage, t.status.kind, t.spec_path, t.track) == (
        Stage.PLAN, Kind.RUNNING, SPEC_PATH, "deep") and at_gate(t)
    assert operator_request(t) is None


def test_dead_session_is_crash(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.repositories[0].worktrees_path) / "task-42"
    (wt / ".agent").mkdir(parents=True)
    save(c.state_dir, TaskState(issue=42, repository="portfolio_eval",
                                stage=Stage.IMPLEMENT, slot=0, worktree=str(wt),
                                branch="agent/task-42", title="t", track="standard",
                                updated_at="2026-07-14T00:00:00+00:00"))
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive=set()))
    main.run_pass(c, d)
    assert gh.released == [(42, "session crashed mid-stage")]
    assert "session_crashed" in d.notifier.sent
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED
    assert wt.exists()  # worktree preserved


def test_dead_session_files_diagnosis_issue_and_blocks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(alive=set()))
    main.run_pass(c, d)

    repo, title, body = gh.created_issues[0]
    assert repo == "jesdi/portfolio_eval", "session crashes file on the REPOSITORY repo"
    assert "session-crash" in title and "implement" in title
    assert "- class: session-crash" in body
    assert "…pane tail…" in body  # FakeSessions.capture_tail
    assert gh.blocked_by == [(42, 501)]
    assert "task_failed" in d.notifier.sent
    # existing crash handling still intact
    assert gh.released == [(42, "session crashed mid-stage")]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.FAILED


@pytest.mark.parametrize("pick, cli", [
    ("anthropic/claude-opus-5", "claude --resume recorded-session"),
    ("openai/gpt-5-codex@high", "codex resume recorded-session"),
])
def test_crash_repro_resumes_on_the_stages_runtime(tmp_path, monkeypatch, pick, cli):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    wt = make_task(c, issue=42, stage=Stage.IMPLEMENT, picks={"implement": pick})
    gh = FakeGitHub()
    main.run_pass(c, deps(gh, FakeSessions(alive=set())))
    body = gh.created_issues[0][2]
    assert f"- repro: `cd {wt} && {cli}  # inside session image`" in body


def test_crash_path_ends_the_session_after_reporting(tmp_path, monkeypatch):
    """HandleCrash must call end() after _report_session_crash so the pane
    tail is captured first (for the web console) and the dead tab is closed."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT)
    gh = FakeGitHub()
    sess = FakeSessions(alive=set())  # session reads dead → HandleCrash
    d = deps(gh, sess)
    main.run_pass(c, d)

    # end() must have been called for (repository, issue)
    assert ("portfolio_eval", 42) in sess.end_calls, "end() not called on crash"

    # capture_tail must precede end in the unified call log
    ct_idx = next(
        (i for i, e in enumerate(sess.call_log) if e[0] == "capture_tail" and e[2] == 42),
        None,
    )
    end_idx = next(
        (i for i, e in enumerate(sess.call_log) if e[0] == "end" and e[2] == 42),
        None,
    )
    assert ct_idx is not None, "capture_tail was never called"
    assert end_idx is not None, "end() was never called"
    assert ct_idx < end_idx, "capture_tail must precede end() in call log"


def test_broken_worktree_task_crash_dedupes_to_one_issue(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=waking(), slot=0)
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(resume_raises=[42]))
    main.run_pass(c, d)
    # Second pass: the task is FAILED now, so it is not woken again — but a
    # re-park by the operator must not file a second identical issue.
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", 42),
                                 stage=Stage.IMPLEMENT, status=waking()))
    main.run_pass(c, d)
    assert len(gh.created_issues) == 1
    assert d.notifier.sent.count("task_failed") == 1


def test_crash_appends_failed_event(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT)
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(alive=set())))
    failed = [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["issue"] == 42
    assert failed[0]["detail"] == "session crashed mid-stage"


# ---------------------------------------------------------------------------
# Slice 10 lock-ins: admission-denied wake retains operator_request
# ---------------------------------------------------------------------------

def test_admission_budget_denied_retains_operator_request(tmp_path):
    """Slice 10 regression lock: DENY_ALL leaves operator_request intact.

    Passes by construction:
    - _wake replaces only park/hold_for_attach/updated_at — operator_request untouched.
    - _drive_wakes skips every task when admit returns denied,
      so no task state is modified at all.
    """
    c = cfg(tmp_path)
    req = AnswersRequest(path=".agent/questionnaire.md")
    make_task(c, issue=42, status=answers(req))
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._wake(c, task, "please answer")
    d = deps()
    main._drive_wakes(c, d, admit=DENY_ALL, order=tuple)
    saved = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(saved) == req   # untouched
    assert saved.status.kind is Kind.WAKING         # woken, not resumed
    assert not d.sessions.resumed          # no resume executed


def test_admission_capacity_full_retains_operator_request(tmp_path):
    """Slice 10 regression lock: capacity-full _mark_wake_blocked leaves operator_request intact.

    Passes by construction:
    - _wake replaces only park/hold_for_attach/updated_at — operator_request untouched.
    - _mark_wake_blocked (main.py:835) only writes a filesystem marker and logs;
      it never calls save() or modifies any TaskState.
    """
    c = replace_capacity(cfg(tmp_path), 1)
    req = AnswersRequest(path=".agent/questionnaire.md")
    make_task(c, issue=42, status=answers(req))
    make_task(c, issue=43, status=RUNNING, stage=Stage.IMPLEMENT)  # active, fills capacity
    task = load(c.state_dir, "portfolio_eval", 42)
    stage_launch._wake(c, task, "please answer")
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    saved = load(c.state_dir, "portfolio_eval", 42)
    assert operator_request(saved) == req   # untouched
    assert saved.status.kind is Kind.WAKING         # still wake-queued, not resumed
    assert not d.sessions.resumed          # no resume executed


def test_handle_crash_failed_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15a-iii: HandleCrash (dead session, _drive_task ~L1147) clears operator_request."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.IMPLEMENT, spec_path="docs/specs/design.md")
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(alive=set())))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED
    assert operator_request(t) is None, (
        f"HandleCrash FAILED must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"


def test_fail_task_crash_clears_operator_request(tmp_path, monkeypatch):
    """Slice 15a-iv: _fail_task_crash (_drive_task raises, ~L984) clears operator_request.
    Triggered via spawn_raises: _spawn_stage propagates the exception out of _drive_task,
    which run_pass catches and routes to _fail_task_crash."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.PLAN, status=held(PlanApprovalRequest(".agent/review.html")),
              spec_path="docs/specs/design.md")
    main.run_pass(c, deps(FakeGitHub(), FakeSessions(spawn_raises=[42])))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.FAILED
    assert operator_request(t) is None, (
        f"_fail_task_crash must clear operator_request, got {operator_request(t)!r}")
    assert t.spec_path == "docs/specs/design.md", "spec_path must be preserved"


def _turn(c, **kw):
    return executor._Turn(c, deps(), c.repositories[0], None, None, False, **kw)


def test_a_refused_event_writes_transition_rejected_with_the_reason(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN)
    task = load(c.state_dir, "portfolio_eval", 42)
    executor._apply(_turn(c, actor="operator"), task, Event(EventType.OP_PARK))
    [e] = _events(c, "transition-rejected")
    assert (e["issue"], e["actor"]) == (42, "operator")
    assert e["transition"] == {"event": "op-park", "from": "parked-pr", "stage": "pr-open"}
    assert e["detail"] == "only a running task or one at the plan gate can be parked"
    assert load(c.state_dir, "portfolio_eval", 42) == task


def test_an_ignored_event_writes_nothing(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c)
    executor._apply(_turn(c), load(c.state_dir, "portfolio_eval", 42), Event(EventType.GRACE_EXPIRED))
    assert _events(c, "transition-rejected") == [] and _events(c, "transition") == []


def test_a_transition_event_records_the_turns_actor(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c, stage=Stage.REVIEW)
    executor._apply(_turn(c, actor="operator"), load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.OP_PARK))
    [e] = _events(c, "transition")
    assert (e["actor"], e["transition"]["to"]) == ("operator", "parked-human")


def _operator_turn(c, gh):
    return executor._Turn(c, deps(gh), c.repositories[0], None, None, False, actor="operator")


def test_op_kill_releases_the_card_and_writes_the_killed_event(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c)
    gh = FakeGitHub()
    executor._apply(_operator_turn(c, gh), load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.OP_KILL))
    assert gh.released == [(42, "abandoned by operator")]
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.status.reason) == (Kind.FAILED, FailReason.KILLED)
    [e] = _events(c, "failed")
    assert (e["detail"], e["actor"], e["stage"]) == ("killed by operator", "operator", "implement")


def test_op_cancel_cancels_the_issue_and_writes_the_canceled_event(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c)
    gh = FakeGitHub()
    executor._apply(_operator_turn(c, gh), load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.OP_CANCEL))
    assert gh.canceled == [42]
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.CANCELED
    [e] = _events(c, "canceled")
    assert (e["detail"], e["actor"], e["stage"]) == ("canceled by operator", "operator", "implement")


def test_op_reply_resets_the_rounds_an_operator_wake_owns(tmp_path):
    from dispatcher.lifecycle import Event, EventType
    c = cfg(tmp_path)
    make_task(c, status=human(), review_rounds=3, ci_rounds=2, unattended_rounds=4)
    executor._apply(_operator_turn(c, FakeGitHub()), load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.OP_REPLY))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.review_rounds, t.ci_rounds, t.unattended_rounds) == (0, 0, -1)


def test_op_resume_sets_the_wakes_model_override_and_usage_bypass(tmp_path):
    from dispatcher.lifecycle import Event, EventType, ResumeChoice
    c = cfg(tmp_path)
    make_task(c, status=human())
    executor._apply(_operator_turn(c, FakeGitHub()), load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.OP_RESUME, ResumeChoice(model="m-x", bypass_usage=True)))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING
    assert (t.resume_model_override, t.resume_bypass_usage) == ("m-x", True)


def _resume_pass(c, **payload):
    """A resume intent the way the web writes it, then one pass whose usage
    gate holds every wake back, so the pass leaves what the intent made."""
    intents_mod.write_intent(c.state_dir, "resume", "portfolio_eval", 42, payload,
                             "jesdi@github", 1)
    main.run_pass(c, deps(sess=FakeSessions()))
    return load(c.state_dir, "portfolio_eval", 42)


@pytest.mark.parametrize("stage,reason", [(Stage.PLAN, "killed"),
                                          (Stage.IMPLEMENT, "session-crash")])
def test_resume_of_a_killed_or_crashed_task_wakes_it(tmp_path, monkeypatch, stage, reason):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=stage, status=failed(reason), slot=NO_SLOT,
              updated_at=datetime.now(timezone.utc).isoformat())  # inside retention
    t = _resume_pass(c)
    assert (t.stage, t.status) == (stage, waking(failed(reason), "resume"))
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []
    assert [e["transition"]["event"] for e in _events(c, "transition")] == ["op-resume"]


def test_resume_on_a_task_that_is_already_waking_replaces_its_model(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    status = waking(human("decision"), "reply")
    make_task(c, stage=Stage.IMPLEMENT, status=status, resume_model_override="old/model")
    before = load(c.state_dir, "portfolio_eval", 42)
    t = _resume_pass(c, model="anthropic/claude-opus-5")
    assert (t.status, t.resume_model_override, t.updated_at) == (
        status, "anthropic/claude-opus-5", before.updated_at)
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_resume_of_a_closed_pr_failure_the_web_accepted_is_rejected(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN, status=failed("pr-closed"), slot=NO_SLOT,
              updated_at=datetime.now(timezone.utc).isoformat())  # inside retention
    t = _resume_pass(c)
    assert t.status == failed("pr-closed")
    [e] = _events(c, "transition-rejected")
    assert (e["transition"]["event"], e["detail"]) == (
        "op-resume", "reopen the PR on GitHub or re-queue the issue")
    from dispatcher import messages
    assert messages.undelivered(c.state_dir, "portfolio_eval", 42) == []


def test_a_refused_resume_is_rejected_whatever_its_model(tmp_path, monkeypatch):
    """The table's refusal wins over the model check: transition-rejected,
    and neither intent-dropped nor intent-applied."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN, status=failed("pr-closed"), slot=NO_SLOT,
              updated_at=datetime.now(timezone.utc).isoformat())  # inside retention
    t = _resume_pass(c, model="openai/gpt-nope")
    assert t.status == failed("pr-closed")
    [e] = _events(c, "transition-rejected")
    assert e["detail"] == "reopen the PR on GitHub or re-queue the issue"
    assert _events(c, "intent-dropped") == [] and _events(c, "intent-applied") == []


def test_the_resume_text_is_stored_with_the_wake_and_sent_at_launch(tmp_path, monkeypatch):
    """No message is queued between the wake's save and its launch: the
    launch of a resume wake writes the text itself, so nothing is lost
    between the two."""
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, issue=42, status=human(), park_msg_id=55)
    t = _resume_pass(c, text="ship it")
    from dispatcher import messages
    assert messages.all_messages(c.state_dir, "portfolio_eval", 42) == []
    assert (t.status.kind, t.resume_text) == (Kind.WAKING, "ship it")
    patch_usage(monkeypatch)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert "ship it" in sess.resumed[0][1]


def test_a_park_intent_without_a_task_is_skipped(tmp_path):
    c = cfg(tmp_path)
    intents_mod.write_intent(c.state_dir, "park", "portfolio_eval", 42, {}, "op", 1)
    operator_intents._apply_intents(c, deps())
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert _events(c, "transition") == [] and _events(c, "transition-rejected") == []


def _admitted_turn(c, sess, stage):
    """A turn whose resume-admitted launch of `stage` the gate admitted."""
    from dispatcher.models import Entry
    turn = _turn(c)
    turn.deps = deps(sess=sess)
    turn.launches[stage] = launch_choice.Launch(stage, Entry("anthropic", "claude-opus-5"))
    return turn


def _resume_admitted(turn, c):
    from dispatcher.lifecycle import Event, EventType, Woken
    executor._apply(turn, load(c.state_dir, "portfolio_eval", 42),
                Event(EventType.RESUME_ADMITTED, Woken("")))
    return load(c.state_dir, "portfolio_eval", 42)


def _ended_before(sess, method):
    """`method` of `sess` asserts the pane was ended before it ran."""
    real = getattr(sess, method)

    def guarded(repository, issue, *a, **kw):
        assert issue in sess.ended, f"{method} over a live pane"
        return real(repository, issue, *a, **kw)
    setattr(sess, method, guarded)


def test_resume_admitted_continues_the_parked_session_after_ending_its_pane(tmp_path):
    c = cfg(tmp_path)
    make_task(c, status=waking(human(), "resume"), resume_text="yes, Postgres",
              resume_model_override="m-x", resume_bypass_usage=True)
    sess = FakeSessions(alive=[42])
    _ended_before(sess, "resume")
    t = _resume_admitted(_admitted_turn(c, sess, Stage.IMPLEMENT), c)
    [(issue, text, model, _)] = sess.resumed
    assert (issue, model) == (42, "anthropic/claude-opus-5") and "yes, Postgres" in text
    assert sess.spawned == []
    assert (t.stage, t.status.kind) == (Stage.IMPLEMENT, Kind.RUNNING)
    assert (t.resume_text, t.resume_model_override, t.resume_bypass_usage) == ("", "", False)
    assert t.picks["implement"] == "anthropic/claude-opus-5"
    [e] = _events(c, "resumed")
    assert (e["stage"], e["detail"]) == ("implement", "")


def test_resume_admitted_on_pr_open_spawns_an_operator_address_review_round(tmp_path):
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PR_OPEN, status=waking(PR, "resume"), resume_text="rename it",
              resume_model_override="m-x")
    sess = FakeSessions()
    t = _resume_admitted(_admitted_turn(c, sess, Stage.ADDRESS_REVIEW), c)
    [(issue, stage, _model, prompt, _)] = sess.spawned
    assert (issue, stage) == (42, "address-review") and "rename it" in prompt
    assert sess.resumed == [] and sess.ended == [42]   # ended before the launch
    assert (t.stage, t.status.kind) == (Stage.ADDRESS_REVIEW, Kind.RUNNING)
    assert "Reason for this round: operator" in prompt
    assert t.feedback_cursor and t.round_reason == "operator"
    assert (t.resume_text, t.resume_model_override) == ("", "")
    [e] = _events(c, "resumed")
    assert e["stage"] == "address-review"


def test_resume_admitted_respawn_never_continues_the_transcript(tmp_path):
    c = cfg(tmp_path)
    make_task(c, status=waking(failed("session-crash"), "resume"))
    sess = FakeSessions(alive=[42])
    _ended_before(sess, "spawn_stage")
    t = _resume_admitted(_admitted_turn(c, sess, Stage.IMPLEMENT), c)
    assert sess.resumed == [], "a respawn never passes --continue"
    assert [(i, s) for i, s, *_ in sess.spawned] == [(42, "implement")]
    assert (t.stage, t.status.kind) == (Stage.IMPLEMENT, Kind.RUNNING)
