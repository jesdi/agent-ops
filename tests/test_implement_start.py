"""The implement start is one admitted transition: a denied pass mutates
nothing, and the admitted pass starts exactly one session for every ticket."""
import json

import pytest

from dispatcher import eventlog, intents, main, messages
from dispatcher.state import (PlanApprovalRequest, Stage, load, read_stage_signal,
                              resumable_crash)
from tests.test_main import FakeSessions, cfg, deps, make_task, patch_usage, write_tickets

REQUEST = PlanApprovalRequest(".agent/review.html")


def _approved(tmp_path, tickets=3, sessions=None, **task_fields):
    """A task at the plan gate whose session reports the operator's approval."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.AWAITING_PLAN_REVIEW, **task_fields)
    write_tickets(wt, tickets)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "plan", "status": "done", "note": "approved", "artifact": ".agent/tickets",
    }))
    sessions = sessions or FakeSessions(alive={42})
    return config, wt, sessions, deps(sess=sessions)


def _task(config):
    return load(config.state_dir, "portfolio_eval", 42)


def _events(config, name):
    return [e for e in eventlog.read_tail(config.state_dir) if e["event"] == name]


@pytest.mark.parametrize("denied_passes", [1, 3], ids=["one-denial", "repeated-denials"])
def test_denied_start_mutates_nothing_and_recovery_starts_one_session(
    tmp_path, monkeypatch, denied_passes,
):
    config, wt, sessions, dependencies = _approved(tmp_path)

    patch_usage(monkeypatch, util=0.99)
    for _ in range(denied_passes):
        main.run_pass(config, dependencies)
    assert sessions.spawned == [] and sessions.ended == []
    task = _task(config)
    assert (task.stage, task.ticket_count) == (Stage.AWAITING_PLAN_REVIEW, 0)
    assert read_stage_signal(wt).status == "done"
    assert _events(config, "stage-started") == []

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)

    assert [(s[0], s[1]) for s in sessions.spawned] == [(42, "implement")]
    task = _task(config)
    assert (task.stage, task.ticket_count) == (Stage.IMPLEMENT, 3)
    assert len(_events(config, "stage-started")) == 1


def test_working_implement_pass_is_a_noop(tmp_path, monkeypatch):
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "working", "note": "1/2 tickets merged",
    }))
    sessions = FakeSessions(alive={42})
    patch_usage(monkeypatch, util=0.2)

    main.run_pass(config, deps(sess=sessions))

    assert sessions.spawned == [] and sessions.ended == []
    assert _task(config).stage == Stage.IMPLEMENT


def test_implement_done_launches_review_deferred_under_denial(tmp_path, monkeypatch):
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "2/2 tickets merged",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    assert sessions.spawned == []
    assert _task(config).stage == Stage.IMPLEMENT

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    assert [(s[0], s[1]) for s in sessions.spawned] == [(42, "review")]
    assert _task(config).stage == Stage.REVIEW


def test_launcher_raise_does_not_record_the_start(tmp_path, monkeypatch):
    """spawn_stage raising (e.g. missing worktree .git): the start did not
    commit, so no stage start is logged and nobody is told implement runs.
    The task fails as crashed in implement, with the approved set's size."""
    config, _wt, _sessions, dependencies = _approved(
        tmp_path, sessions=FakeSessions(alive={42}, spawn_raises={42}))
    patch_usage(monkeypatch, util=0.2)

    main.run_pass(config, dependencies)

    assert _events(config, "stage-started") == []
    assert "implement_started" not in dependencies.notifier.sent
    task = _task(config)
    assert (task.stage, task.crashed_stage, task.ticket_count) == (
        Stage.FAILED, "implement", 3)
    assert len(_events(config, "failed")) == 1


def test_resume_after_a_failed_implement_launch_needs_no_second_approval(
        tmp_path, monkeypatch):
    """The approval was accepted in the pass whose launch failed: Resume
    starts the implement session, on the track the approval named."""
    config, _wt, _sessions, dependencies = _approved(
        tmp_path, sessions=FakeSessions(alive={42}, spawn_raises={42}),
        gated=True, asked=True)
    (_wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "track": "deep"}))
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    failed = _task(config)
    assert resumable_crash(failed) and failed.track == "deep"

    intents.write_intent(config.state_dir, "resume", "portfolio_eval", 42, {}, "op", 1)
    sessions = FakeSessions()
    d = deps(sess=sessions)
    main.run_pass(config, d)
    sessions.alive_set = {42}
    main.run_pass(config, d)

    assert [(s[0], s[1], s[2]) for s in sessions.spawned] == [
        (42, "implement", "anthropic/claude-opus-5")]
    assert sessions.spawned[0][4] == "medium"       # the deep track's entry
    task = _task(config)
    assert (task.stage, task.crashed_stage, task.operator_request) == (
        Stage.IMPLEMENT, "", None)
    assert (task.gated, task.asked, task.ticket_count) == (True, True, 3)
    assert not [n for n in d.notifier.sent if "review" in n or "parked" in n]


def test_counters_retained_on_denial_reset_on_start(tmp_path, monkeypatch):
    """A denied pass leaves all counters untouched; the admitted start zeroes
    review/gate/e2e rounds (loops.reset(STAGE_STARTED)) and retains ci_rounds."""
    config, _wt, _sessions, dependencies = _approved(
        tmp_path, review_rounds=1, gate_rounds=1, e2e_rounds=1, ci_rounds=2)

    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    t = _task(config)
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 1, 1, 2)

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    t = _task(config)
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds) == (0, 0, 0)
    assert t.ci_rounds == 2  # ci belongs to the PR, not the stage


def test_operator_request_preserved_on_denial_cleared_on_start(tmp_path, monkeypatch):
    config, _wt, _sessions, dependencies = _approved(
        tmp_path, operator_request=REQUEST, spec_path="specs/x/spec.md")

    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    t = _task(config)
    assert (t.operator_request, t.spec_path) == (REQUEST, "specs/x/spec.md")

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    t = _task(config)
    assert (t.operator_request, t.spec_path) == (None, "specs/x/spec.md")


def test_queued_messages_delivered_only_after_successful_start(tmp_path, monkeypatch):
    config, _wt, _sessions, dependencies = _approved(tmp_path)
    messages.append(config.state_dir, "portfolio_eval", 42, "use the staging URL", "jesdi@github")

    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    assert [m.text for m in messages.undelivered(config.state_dir, "portfolio_eval", 42)] == [
        "use the staging URL"]

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    assert messages.undelivered(config.state_dir, "portfolio_eval", 42) == []
    all_msgs = messages.all_messages(config.state_dir, "portfolio_eval", 42)
    assert len(all_msgs) == 1 and all_msgs[0].delivered_at != ""


@pytest.mark.parametrize("loop,rnd", [("review", 3), ("e2e", 4), ("ci", 4)])
def test_no_round_an_implement_session_reports_parks_it(tmp_path, monkeypatch, loop, rnd):
    """implement-spec has its own review loop of four rounds: the dispatcher's
    caps never apply to a round the implement session names."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_count=2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "working", "loop": loop, "round": rnd,
        "note": "1/2 tickets merged"}))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)
    patch_usage(monkeypatch, util=0.2)

    main.run_pass(config, dependencies)

    task = _task(config)
    assert (task.stage, task.park) == (Stage.IMPLEMENT, "")
    assert (task.review_rounds, task.e2e_rounds, task.ci_rounds) == (0, 0, 0)
    assert sessions.ended == []
    assert not {"parked_question", "last_round"} & set(dependencies.notifier.sent)
