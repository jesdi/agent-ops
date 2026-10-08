"""The provider record is written when the implement session launches. These tests read the RAW state file, not `state.load`: the reader
derives a missing record from the implement pick, which hides a lost one."""
import json

import dispatcher.main as main
from dispatcher import execution_overrides, state
from dispatcher.state import Stage
from tests.test_main import FakeSessions, deps, make_task, write_tickets
from tests.test_pinned_tracks_order import (OPUS, SOL, ahead, enter, make_cfg,
                                            run_pass)


def raw_providers(c, issue=42):
    p = state._path(c.state_dir, "portfolio_eval", issue)
    return json.loads(p.read_text()).get("implement_providers")


def test_the_implement_session_records_its_provider(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    enter(c, 42, "standard", Stage.PLAN)
    run_pass(c, 42)
    assert raw_providers(c) == ["openai"]


def test_a_provider_is_recorded_once_through_review(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    wt = make_task(c, issue=42, stage=Stage.AWAITING_PLAN_REVIEW,
                   track="standard")
    write_tickets(wt, 3)
    sess = FakeSessions(alive={42})
    for sig in [{"stage": "plan", "status": "done", "note": "3 tickets",
                 "artifact": ".agent/tickets"},
                {"stage": "implement", "status": "done"}]:
        (wt / ".agent" / "stage.json").write_text(json.dumps(sig))
        main.run_pass(c, deps(sess=sess))
    assert [s[1] for s in sess.spawned] == ["implement", "review"]
    assert raw_providers(c) == ["openai"]


def test_an_override_records_the_overriding_provider(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    enter(c, 42, "standard", Stage.PLAN)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 42,
        execution_overrides.ExecutionOverride(model=OPUS, bypass_usage=False))
    run_pass(c, 42)
    assert raw_providers(c) == ["anthropic"]


def test_an_implement_launch_that_raises_records_nothing(tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    enter(c, 42, "standard", Stage.PLAN)
    main.run_pass(c, deps(sess=FakeSessions(alive={42}, spawn_raises=[42])))
    assert not raw_providers(c)
