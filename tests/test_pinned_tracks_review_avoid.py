"""Acceptance tests for ticket 05 of pinned-tracks: review avoids the one
provider that ran the task's tickets, and avoids none when tickets ran on
two. Black-box through run_pass (launches from the sessions fake, state from
dispatcher.state). The recorded field is `implement_providers` (design.md).
Policy, mode and usage come from tests/test_pinned_tracks_order.py."""
import json
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher import state
from dispatcher.state import PARK_WAKE, Stage, load
from tests.test_main import (FakeSessions, deps, make_task, record_session,
                             write_tickets)
from tests.test_pinned_tracks_order import (ASTRA, FABLE, OPUS, SOL, SONNET,
                                            ahead, enter, launched, make_cfg,
                                            run_pass)

BOTH = ["openai", "anthropic"]


def saved(c, issue=42):
    return load(c.state_dir, "portfolio_eval", issue)


def review_after(tmp_path, monkeypatch, track, mode, implement_pick,
                 providers):
    """A task whose implement session signalled done, providers given in state."""
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode=mode)
    enter(c, 42, track, Stage.IMPLEMENT, ticket_count=1,
          picks={"implement": implement_pick},
          implement_providers=list(providers))
    return c, run_pass(c, 42)


def test_implement_on_one_provider_reviews_on_the_other(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    wt = make_task(c, issue=42, stage=Stage.AWAITING_PLAN_REVIEW,
                   track="standard")
    write_tickets(wt, 3)
    sess = FakeSessions(alive={42})
    signals = [{"stage": "plan", "status": "done", "note": "3 tickets",
                "artifact": ".agent/tickets"},
               {"stage": "implement", "status": "done"}]
    for sig in signals:
        (wt / ".agent" / "stage.json").write_text(json.dumps(sig))
        main.run_pass(c, deps(sess=sess))
    assert launched(sess) == [(SOL, "medium"), (f"anthropic/{OPUS}", "medium")]
    assert saved(c).implement_providers == ["openai"]


def test_architecture_with_both_providers_keeps_written_order(
        tmp_path, monkeypatch):
    c, sess = review_after(tmp_path, monkeypatch, "architecture", None,
                           f"anthropic/{FABLE}@medium", BOTH)
    assert launched(sess) == [(f"anthropic/{FABLE}", "high")]
    assert saved(c).implement_providers == BOTH


def test_standard_both_providers_mode_anthropic_follows_mode(
        tmp_path, monkeypatch):
    c, sess = review_after(tmp_path, monkeypatch, "standard", "anthropic",
                           f"anthropic/{SONNET}@medium", BOTH)
    assert launched(sess) == [(f"anthropic/{OPUS}", "medium")]


def test_standard_both_providers_mode_openai_follows_mode(
        tmp_path, monkeypatch):
    c, sess = review_after(tmp_path, monkeypatch, "standard", "openai",
                           f"{SOL}@medium", BOTH)
    assert launched(sess) == [(ASTRA, "medium")]


def test_two_models_of_one_provider_record_one_provider(
        tmp_path, monkeypatch):
    c, sess = review_after(tmp_path, monkeypatch, "frontend", None,
                           f"anthropic/{OPUS}@medium", ["anthropic"])
    assert launched(sess) == [(ASTRA, "medium")]
    assert saved(c).implement_providers == ["anthropic"]


def test_a_parked_implement_session_resumes_on_its_pick_without_a_second_provider(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    wt = make_task(c, issue=42, stage=Stage.AWAITING_PLAN_REVIEW,
                   track="standard")
    write_tickets(wt, 2)
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "plan", "status": "done", "note": "2 tickets",
         "artifact": ".agent/tickets"}))
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    assert saved(c).implement_providers == ["openai"]
    state.save(c.state_dir, dc_replace(saved(c), park=PARK_WAKE))
    record_session(c, "implement")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "blocked", "note": "q"}))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [r[2] for r in sess.resumed] == [SOL]
    assert saved(c).implement_providers == ["openai"]


def test_old_shape_state_enters_review_with_openai_moved_back(
        tmp_path, monkeypatch):
    c = make_cfg(tmp_path, monkeypatch, ahead(), mode="openai")
    enter(c, 42, "standard", Stage.IMPLEMENT, ticket_count=1,
          picks={"implement": f"{SOL}@medium"})
    p = state._path(c.state_dir, "portfolio_eval", 42)
    raw = json.loads(p.read_text())
    raw.pop("implement_providers", None)
    p.write_text(json.dumps(raw))
    assert "implement_providers" not in json.loads(p.read_text())
    sess = run_pass(c, 42)
    assert launched(sess) == [(f"anthropic/{OPUS}", "medium")]
    # Nothing was recorded for it: the reader falls back to the implement pick.
    assert saved(c).implement_providers in ([], ["openai"])
