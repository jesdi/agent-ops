"""First red regression for ticket starts deferred by execution admission."""
import json

import pytest

from dispatcher import eventlog, main
from dispatcher.state import Stage, load, read_stage_signal
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
    issue, stage, _model, prompt = sessions.spawned[0]
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
