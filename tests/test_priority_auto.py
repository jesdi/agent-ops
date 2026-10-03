"""Acceptance tests for ticket 01 (provider priority): with no stored mode a
stage with no pick launches on the admitted entry whose weekly quota most needs
spending (highest required pace). Black-box: run_pass launches (read from the
sessions fake and TaskState.picks) and dispatcher.usage.required_pace.

Admission is not under test here, so unless a case says otherwise the pace
margin is 1.0 (every weekly window with quota left is admitted) and
weekend_weight is 1.0, which makes required pace plain remaining/remaining-time.
Stage lists are tried in both written orders where the outcome must not depend
on the written order."""
import json
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides
from dispatcher.models import parse_policy
from dispatcher.state import Stage, load
from dispatcher.usage import (PaceConfig, ProviderUsage, Window, WindowKind,
                              admits, allowance, unavailable)
from tests.test_main import (FakeSessions, cfg, deps, make_task,
                             write_tickets)

SONNET = "claude-sonnet-5-5"
LUNA = "openai/gpt-6-luna@high"
OPUS = "claude-opus-5-5"
SOL = "openai/gpt-6-sol"
FABLE = "claude-fable-5-1"
WRITTEN = [SONNET, LUNA]
FLIPPED = [LUNA, SONNET]
BOTH_ORDERS = pytest.mark.parametrize("stage_list", [WRITTEN, FLIPPED],
                                      ids=["sonnet-first", "luna-first"])
PACE = PaceConfig(weekend_weight=1.0, pace_margin=1.0)
S, W_ = WindowKind.SESSION, WindowKind.WEEKLY


def now_utc():
    return datetime.now(timezone.utc)


def wk(used, hours, scope=None, now=None):
    return Window(W_, scope, used, (now or now_utc()) + timedelta(hours=hours))


def ses(used, hours=2.0, now=None):
    return Window(S, None, used, (now or now_utc()) + timedelta(hours=hours))


def usage(provider, *windows):
    return ProviderUsage(provider, "oauth", time.time(), tuple(windows))


def both(anthropic_windows, openai_windows):
    return {"anthropic": usage("anthropic", *anthropic_windows),
            "openai": usage("openai", *openai_windows)}


def policy(implement, review=None):
    track = {"when": "x", "spec": implement, "plan": implement,
             "implement": implement, "review": review or implement}
    return parse_policy({"triage": [SONNET], "untracked": "standard",
                         "tracks": {"standard": track}})


def run(tmp_path, monkeypatch, usages, implement=WRITTEN, review=None,
        pace=PACE, stage=Stage.PLAN, picks=None, override=None, frozen=None,
        **task_kw):
    """One pass over a task about to launch its next stage: PLAN done enters
    implement; IMPLEMENT done (one ticket) enters review. Returns the sessions
    fake and the task as saved after the pass."""
    c = dc_replace(cfg(tmp_path), models=policy(implement, review), pace=pace)
    monkeypatch.setattr(main, "fetch_all", lambda cfg, **k: usages)
    if frozen is not None:
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen
        monkeypatch.setattr(main, "datetime", Frozen)
    wt = make_task(c, issue=42, stage=stage, picks=picks or {}, **task_kw)
    if stage is Stage.PLAN:
        write_tickets(wt, 1)
        signal = {"stage": "plan", "status": "done", "note": "1 ticket",
                  "artifact": ".agent/tickets"}
    else:
        write_tickets(wt, task_kw.get("ticket_count", 1))
        signal = {"stage": "implement", "status": "done"}
    (wt / ".agent" / "stage.json").write_text(json.dumps(signal))
    if override:
        execution_overrides.save(c.state_dir, "portfolio_eval", 42, override)
    sess = FakeSessions(alive={42})
    main.run_pass(c, deps(sess=sess))
    return sess, load(c.state_dir, "portfolio_eval", 42)


def launched(sess):
    return [s[2] for s in sess.spawned]


# --- criterion: highest required pace wins -------------------------------

@BOTH_ORDERS
def test_openai_with_the_higher_required_pace_launches_first_5_6_vs_1_6(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.10, 96)], [wk(0.20, 24)])
    sess, t = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["openai/gpt-6-luna"]
    assert sess.spawned[0][4] == "high"
    assert t.picks["implement"] == LUNA


@BOTH_ORDERS
def test_openai_with_the_higher_required_pace_launches_first_3_0_vs_1_6(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.20, 84)], [wk(0.70, 16.8)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["openai/gpt-6-luna"]


@pytest.mark.parametrize("stage_list", [WRITTEN, FLIPPED],
                         ids=["sonnet-first", "luna-first"])
def test_equal_required_pace_keeps_the_written_order(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.50, 84)], [wk(0.50, 84)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == [
        "anthropic/claude-sonnet-5-5" if stage_list[0] == SONNET
        else "openai/gpt-6-luna"]


@BOTH_ORDERS
def test_weekend_weighted_clock_makes_anthropic_2_4_beat_openai_2_0(
        tmp_path, monkeypatch, stage_list):
    sat = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    u = both([wk(0.60, 48, now=sat)], [wk(0.0, 96, now=sat)])
    pace = PaceConfig(weekend_weight=0.5, timezone="UTC", pace_margin=1.0)
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list, pace=pace,
                  frozen=sat)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


# --- criterion: required_pace arithmetic ---------------------------------

SAT = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
WEEKEND_HALF = PaceConfig(weekend_weight=0.5, timezone="UTC")


def test_required_pace_weekend_weighted_anthropic_about_2_4():
    from dispatcher.usage import required_pace
    w = Window(W_, None, 0.60, datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert required_pace(w, SAT, WEEKEND_HALF) == pytest.approx(2.4, abs=0.01)


def test_required_pace_weekend_weighted_openai_about_2_0():
    from dispatcher.usage import required_pace
    w = Window(W_, None, 0.0, datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert required_pace(w, SAT, WEEKEND_HALF) == pytest.approx(2.0, abs=0.01)


def test_required_pace_95_percent_used_resetting_in_1h_is_about_8_4():
    from dispatcher.usage import required_pace
    now = now_utc()
    assert required_pace(wk(0.95, 1, now=now), now, PACE) == pytest.approx(
        8.4, abs=0.05)


def test_required_pace_of_a_session_window_is_none():
    from dispatcher.usage import required_pace
    now = now_utc()
    assert required_pace(ses(0.5, now=now), now, PACE) is None


def test_required_pace_of_a_weekly_window_reset_5_minutes_ago_is_none():
    from dispatcher.usage import required_pace
    now = now_utc()
    assert required_pace(wk(0.5, -5 / 60, now=now), now, PACE) is None


def test_required_pace_of_an_all_weekend_remainder_at_zero_weight_is_none():
    from dispatcher.usage import required_pace
    w = Window(W_, None, 0.5, datetime(2026, 10, 5, tzinfo=timezone.utc))
    zero = PaceConfig(weekend_weight=0.0, timezone="UTC")
    assert required_pace(w, SAT, zero) is None


# --- criterion: nearly-reset window dominates; unrated entries rank last --

@BOTH_ORDERS
def test_openai_95_percent_used_resetting_in_1h_8_4_beats_anthropic_3_0(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.40, 33.6)], [wk(0.95, 1)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["openai/gpt-6-luna"]


def test_entry_whose_weekly_reset_has_passed_ranks_last(tmp_path, monkeypatch):
    u = both([wk(0.90, 84)], [wk(0.50, -5 / 60)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=FLIPPED)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


@pytest.mark.parametrize("review,expected", [
    ([FABLE, SOL], "openai/gpt-6-sol"),
    ([OPUS, SOL], "anthropic/claude-opus-5-5"),
    ([SOL, OPUS], "anthropic/claude-opus-5-5"),
], ids=["fable-scoped-window-binds", "opus-ignores-fable-window",
        "opus-ignores-fable-window-sol-first"])
def test_scoped_weekly_window_counts_only_for_the_model_it_names(
        tmp_path, monkeypatch, review, expected):
    u = both([wk(0.20, 84), wk(0.90, 84, scope="Fable")], [wk(0.50, 84)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=review, review=review)
    assert launched(sess) == [expected]


def test_openai_usage_unavailable_ranks_last_and_is_denied(
        tmp_path, monkeypatch):
    u = {"anthropic": usage("anthropic", wk(0.90, 84)),
         "openai": unavailable("openai", time.time())}
    sess, _ = run(tmp_path, monkeypatch, u, implement=FLIPPED)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


def test_openai_with_only_a_session_window_ranks_last(tmp_path, monkeypatch):
    u = both([wk(0.90, 84)], [ses(0.10)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=FLIPPED)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


# --- criterion: the gate still decides what may run ----------------------

@BOTH_ORDERS
def test_top_ranked_openai_over_its_session_threshold_falls_to_sonnet(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.10, 96)], [wk(0.20, 24), ses(0.85)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


@BOTH_ORDERS
def test_top_ranked_anthropic_at_80_percent_session_is_denied_openai_runs(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.20, 24), ses(0.80)], [wk(0.10, 96)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["openai/gpt-6-luna"]


@BOTH_ORDERS
def test_top_ranked_anthropic_at_79_percent_session_is_admitted_and_runs(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.20, 24), ses(0.79)], [wk(0.10, 96)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


@BOTH_ORDERS
def test_nothing_admitted_launches_nothing_and_records_no_pick(
        tmp_path, monkeypatch, stage_list):
    u = both([wk(0.10, 96), ses(0.95)], [wk(0.20, 24), ses(0.95)])
    sess, t = run(tmp_path, monkeypatch, u, implement=stage_list)
    assert sess.spawned == []
    assert "implement" not in t.picks


# --- criterion: provider is fixed per stage; override wins ---------------

def test_review_avoids_the_implement_provider_although_openai_ranks_first(
        tmp_path, monkeypatch):
    u = both([wk(0.10, 96)], [wk(0.20, 24)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=WRITTEN,
                  review=[OPUS, SOL], stage=Stage.IMPLEMENT,
                  ticket_cursor=1, ticket_count=1, picks={"implement": LUNA})
    assert launched(sess) == ["anthropic/claude-opus-5-5"]


def test_a_sonnet_implement_pick_is_kept_for_the_next_ticket_although_openai_ranks_first(
        tmp_path, monkeypatch):
    u = both([wk(0.10, 96)], [wk(0.20, 24)])
    sess, _ = run(tmp_path, monkeypatch, u, implement=FLIPPED,
                  stage=Stage.IMPLEMENT, ticket_cursor=1, ticket_count=2,
                  picks={"implement": SONNET})
    assert launched(sess) == ["anthropic/claude-sonnet-5-5"]


def test_a_one_shot_override_wins_although_openai_ranks_first(
        tmp_path, monkeypatch):
    u = both([wk(0.10, 96)], [wk(0.20, 24)])
    ov = execution_overrides.ExecutionOverride(model=OPUS, bypass_usage=False)
    sess, _ = run(tmp_path, monkeypatch, u, implement=FLIPPED, override=ov)
    assert launched(sess) == ["anthropic/claude-opus-5-5"]


# --- criterion: the gate's verdict is unchanged ---------------------------
# Pinned from the code as it stands; green by nature, red if allowance,
# readings or admits change for identical usage.

GATE_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # a Thursday


def _gate_usages():
    return {
        "anthropic": usage("anthropic", ses(0.5, 2, GATE_NOW),
                           wk(0.3, 84, now=GATE_NOW),
                           wk(0.9, 84, "Fable", now=GATE_NOW)),
        "openai": usage("openai", ses(0.85, 2, GATE_NOW),
                        wk(0.2, 24, now=GATE_NOW)),
        "gone": unavailable("gone", 0.0),
    }


@pytest.mark.parametrize("pace,model,admitted,reason,binding", [
    (PaceConfig(), "claude-sonnet-5-5", True, "ok", ("session", None, 0.8, 0.3)),
    (PaceConfig(), "claude-fable-5-1", False, "over-pace",
     ("weekly", "Fable", 0.68333, -0.21667)),
    (PaceConfig(), "openai/gpt-6-sol", False, "over-threshold",
     ("session", None, 0.8, -0.05)),
    (PaceConfig(), "gone/x", False, "unavailable", None),
    (PaceConfig(), "nowhere/y", False, "unavailable", None),
    (PaceConfig(weekend_weight=1.0, pace_margin=0.0), "claude-sonnet-5-5",
     True, "ok", ("weekly", None, 0.5, 0.2)),
    (PaceConfig(weekend_weight=1.0, pace_margin=0.0), "claude-fable-5-1",
     False, "over-pace", ("weekly", "Fable", 0.5, -0.4)),
], ids=["sonnet", "fable", "openai", "unavailable", "missing",
        "sonnet-no-margin", "fable-no-margin"])
def test_gate_verdict_is_unchanged_for_identical_usage(
        pace, model, admitted, reason, binding):
    v = admits(_gate_usages(), model, GATE_NOW, pace)
    assert (v.admitted, v.reason) == (admitted, reason)
    if binding is None:
        assert v.binding is None
        return
    kind, scope, allow, headroom = binding
    b = v.binding
    assert (b.window.kind.value, b.window.scope) == (kind, scope)
    assert b.allowance == pytest.approx(allow, abs=1e-4)
    assert b.headroom == pytest.approx(headroom, abs=1e-4)
    assert allowance(b.window, GATE_NOW, pace) == pytest.approx(allow, abs=1e-4)
