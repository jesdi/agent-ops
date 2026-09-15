"""First red regression for ticket starts deferred by execution admission."""
import json

import pytest

from dispatcher import eventlog, main, messages
from dispatcher.state import SpecApprovalRequest, Stage, load, read_stage_signal
from tests.test_main import FakeSessions, cfg, deps, make_task, patch_usage, write_tickets


@pytest.mark.parametrize("denied_passes", [1, 3], ids=["one-denial", "repeated-denials"])
def test_budget_recovery_starts_the_unworked_ticket_before_review(
    tmp_path, monkeypatch, denied_passes,
):
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    patch_usage(monkeypatch, util=0.99)
    for _ in range(denied_passes):
        main.run_pass(config, dependencies)
    assert sessions.spawned == []

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)

    assert len(sessions.spawned) == 1
    issue, stage, _model, prompt, _effort = sessions.spawned[0]
    assert (issue, stage) == (42, "implement")
    assert ".agent/tickets/02-t2.md" in prompt


def _denied_setup(tmp_path, monkeypatch):
    """Shared setup: task at cursor=1/count=2, done signal, single denied pass."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)
    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    return config, wt, sessions, dependencies


def test_denied_pass_leaves_cursor_and_done_signal_unchanged(tmp_path, monkeypatch):
    config, wt, _sessions, _dependencies = _denied_setup(tmp_path, monkeypatch)

    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 1
    assert task.ticket_count == 2

    signal = read_stage_signal(wt)
    assert signal is not None
    assert signal.status == "done"
    assert signal.stage == "implement"


def test_denied_pass_emits_no_ticket_started_and_keeps_session(tmp_path, monkeypatch):
    config, _wt, sessions, _dependencies = _denied_setup(tmp_path, monkeypatch)

    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []

    assert sessions.ended == []


def test_plan_done_denied_then_recovered_starts_ticket_one(tmp_path, monkeypatch):
    """Plan-completion denied by budget later starts ticket 1 with correct set size."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.PLAN)
    write_tickets(wt, 3)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "plan", "status": "done", "note": "3 tickets", "artifact": ".agent/tickets",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    assert sessions.spawned == []
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 0

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    assert len(sessions.spawned) == 1
    issue, stage, _model, prompt, _effort = sessions.spawned[0]
    assert (issue, stage) == (42, "implement")
    assert ".agent/tickets/01-t1.md" in prompt
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 1
    assert task.ticket_count == 3
    assert task.stage == Stage.IMPLEMENT
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert len(started) == 1
    assert started[0]["detail"] == "ticket 1/3"


# ---------------------------------------------------------------------------
# Slice 6: in-flight ticket pass is a no-op
# ---------------------------------------------------------------------------

def test_working_ticket_pass_is_a_noop(tmp_path, monkeypatch):
    """Ticket 2 already in-flight (working signal); a pass must do nothing."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=2, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "working",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)

    assert sessions.spawned == []
    assert sessions.ended == []
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 2
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []


# ---------------------------------------------------------------------------
# Slice 7: last ticket done → REVIEW (admitted) / deferred (denied)
# ---------------------------------------------------------------------------

def test_last_ticket_completion_launches_review_deferred_under_denial(tmp_path, monkeypatch):
    """last ticket done: denied pass defers; admitted pass spawns review."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=2, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 2 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    # Denied pass: no spawn, stage stays IMPLEMENT
    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    assert sessions.spawned == []
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.stage == Stage.IMPLEMENT

    # Admitted pass: spawns review
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    assert len(sessions.spawned) == 1
    issue, stage_name, _model, _prompt, _effort = sessions.spawned[0]
    assert (issue, stage_name) == (42, "review")
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.stage == Stage.REVIEW
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []


# ---------------------------------------------------------------------------
# Slice 8: missing requested ticket → failure, no ticket-started, no review
# ---------------------------------------------------------------------------

def test_missing_requested_ticket_fails_without_start_or_review(tmp_path, monkeypatch):
    """StartTicket for a missing ticket file: task fails, no ticket-started, no review spawn.

    Validation must happen BEFORE ending the previous session — so
    sessions.ended must be empty when the requested ticket file is absent.
    """
    config = cfg(tmp_path)
    # cursor=1, count=2 with ONLY 01-t1.md on disk; 02-t2.md is absent
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 1)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)
    patch_usage(monkeypatch, util=0.2)

    main.run_pass(config, dependencies)

    # No ticket-started event
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []
    # Task did not advance cursor past 1
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 1
    # No review spawn
    assert (42, "review") not in [(i, s) for i, s, _, _, _ in sessions.spawned]
    # Existing failure outcome: task marked FAILED with a "failed" event
    assert task.stage == Stage.FAILED
    failed_events = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "failed"]
    assert len(failed_events) >= 1
    # Previous session must NOT have been ended before the validation fires
    assert sessions.ended == []


# ---------------------------------------------------------------------------
# Slice 9: launcher raising does not record the next ticket as started
# ---------------------------------------------------------------------------

def test_launcher_raise_does_not_record_ticket_started(tmp_path, monkeypatch):
    """spawn_stage raising (e.g. missing worktree .git) must not emit ticket-started
    and must not persist an advanced cursor — the start did not commit.
    """
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    # Launcher raises FileNotFoundError on issue 42
    sessions = FakeSessions(alive={42}, spawn_raises={42})
    dependencies = deps(sess=sessions)
    patch_usage(monkeypatch, util=0.2)

    main.run_pass(config, dependencies)

    # No ticket-started event
    started = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "ticket-started"]
    assert started == []
    # Cursor not advanced to 2
    task = load(config.state_dir, "portfolio_eval", 42)
    assert task.ticket_cursor == 1
    # Existing failure outcome fires
    assert task.stage == Stage.FAILED
    failed_events = [e for e in eventlog.read_tail(config.state_dir) if e["event"] == "failed"]
    assert len(failed_events) >= 1


# ---------------------------------------------------------------------------
# Slice 10: loop counters retained on denial, stage-scoped reset on start
# ---------------------------------------------------------------------------

def test_counters_retained_on_denial_reset_on_start(tmp_path, monkeypatch):
    """StartTicket: denied pass leaves all counters untouched;
    admitted pass delegates to loops.reset(STAGE_STARTED) → zeroes
    review/gate/e2e rounds and retains ci_rounds."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2,
                   review_rounds=1, gate_rounds=1, e2e_rounds=1, ci_rounds=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    # Denied pass: all counters must stay as set
    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    t = load(config.state_dir, "portfolio_eval", 42)
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 1, 1, 2)

    # Admitted pass: stage-scoped counters reset via loops.reset(STAGE_STARTED); ci_rounds retained
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    t = load(config.state_dir, "portfolio_eval", 42)
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds) == (0, 0, 0)
    assert t.ci_rounds == 2  # ci belongs to the PR, not the stage


# ---------------------------------------------------------------------------
# Slice 11: operator_request preserved on denial, cleared on start
# ---------------------------------------------------------------------------

def test_operator_request_preserved_on_denial_cleared_on_start(tmp_path, monkeypatch):
    """StartTicket: denied pass keeps operator_request and spec_path intact;
    admitted pass clears operator_request (via _spawn_stage) and retains spec_path."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2,
                   operator_request=SpecApprovalRequest(),
                   spec_path="docs/specs/x-design.md")
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    # Denied pass: operator_request and spec_path unchanged
    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    t = load(config.state_dir, "portfolio_eval", 42)
    assert t.operator_request == SpecApprovalRequest()
    assert t.spec_path == "docs/specs/x-design.md"

    # Admitted pass: operator_request cleared; spec_path retained (spec_path or task.spec_path fallback)
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    t = load(config.state_dir, "portfolio_eval", 42)
    assert t.operator_request is None
    assert t.spec_path == "docs/specs/x-design.md"


# ---------------------------------------------------------------------------
# Slice 12: queued messages stay undelivered after denial, delivered once on start
# ---------------------------------------------------------------------------

def test_queued_messages_delivered_only_after_successful_start(tmp_path, monkeypatch):
    """StartTicket: denied pass leaves queued messages undelivered;
    admitted pass delivers them exactly once via _spawn_stage → messages.mark_delivered."""
    config = cfg(tmp_path)
    wt = make_task(config, stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2)
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps({
        "stage": "implement", "status": "done", "note": "ticket 1 complete",
    }))
    messages.append(config.state_dir, 42, "use the staging URL", "jesdi@github")
    sessions = FakeSessions(alive={42})
    dependencies = deps(sess=sessions)

    # Denied pass: message still undelivered
    patch_usage(monkeypatch, util=0.99)
    main.run_pass(config, dependencies)
    assert len(messages.undelivered(config.state_dir, 42)) == 1
    assert messages.undelivered(config.state_dir, 42)[0].text == "use the staging URL"

    # Admitted pass: message delivered exactly once
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(config, dependencies)
    assert messages.undelivered(config.state_dir, 42) == []
    all_msgs = messages.all_messages(config.state_dir, 42)
    assert len(all_msgs) == 1
    assert all_msgs[0].delivered_at != ""
