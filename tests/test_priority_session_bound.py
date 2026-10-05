"""Acceptance tests for ticket 02 (provider priority): in auto, a session-bound
provider (remaining weekly quota >= its spendable maximum = session_week_share
x sessions left before the weekly reset) is tried before all others. Black-box:
run_pass launches, dispatcher.usage.session_bound, load_config.

Every case builds its windows from one frozen `now` (also frozen inside
run_pass), so reset distances are exact. Float note: at the 30% boundary,
1 - 0.70 and 0.05 * 6 are both 0.30000000000000004 in IEEE doubles, so the
boundary case holds only for an implementation that compares them as computed
(or with a tolerance); the ticket's numbers leave no exact representation.

PaceConfig(session_week_share=...) is built inside each test body so a missing
field fails the test, not collection."""
from datetime import datetime, timezone

import pytest

from dispatcher.config import load_config
from dispatcher.usage import PaceConfig, admits
from tests.test_config import SAMPLE
from tests.test_priority_auto import (BOTH_ORDERS, FLIPPED, LUNA, OPUS, SOL,
                                      SONNET, WRITTEN, _gate_usages, both,
                                      launched, run, ses, usage, wk, GATE_NOW)
from dispatcher.state import Stage

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)  # a Thursday
SHARE = {"anthropic": 0.05}
OPENAI_5_6 = lambda: [wk(0.20, 24, now=NOW)]  # noqa: E731  5.6x required pace


def pace(share=SHARE):
    return PaceConfig(weekend_weight=1.0, pace_margin=1.0,
                      session_week_share=share)


def go(tmp_path, monkeypatch, anthropic, implement=WRITTEN, share=SHARE, **kw):
    return run(tmp_path, monkeypatch, both(anthropic, OPENAI_5_6()),
               implement=implement, pace=pace(share), frozen=NOW, **kw)


SONNET_ID = "anthropic/claude-sonnet-5-5"
LUNA_ID = "openai/gpt-6-luna"


# --- launch criteria -------------------------------------------------------

@BOTH_ORDERS
def test_session_bound_30_percent_quota_equals_30_percent_maximum_goes_first(
        tmp_path, monkeypatch, stage_list):
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.0, 5, NOW), wk(0.70, 30, now=NOW)], implement=stage_list)
    assert launched(sess) == [SONNET_ID]


@BOTH_ORDERS
def test_quota_29_percent_below_the_30_percent_maximum_is_not_session_bound(
        tmp_path, monkeypatch, stage_list):
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.0, 5, NOW), wk(0.71, 30, now=NOW)], implement=stage_list)
    assert launched(sess) == [LUNA_ID]


@BOTH_ORDERS
def test_open_session_40_percent_used_counts_half_a_session_27_5_vs_28(
        tmp_path, monkeypatch, stage_list):
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.40, 5, NOW), wk(0.72, 30, now=NOW)], implement=stage_list)
    assert launched(sess) == [SONNET_ID]


@BOTH_ORDERS
def test_no_session_window_counts_every_started_5h_period_6_for_28h(
        tmp_path, monkeypatch, stage_list):
    sess, _ = go(tmp_path, monkeypatch, [wk(0.70, 28, now=NOW)],
                 implement=stage_list)
    assert launched(sess) == [SONNET_ID]


@BOTH_ORDERS
def test_session_bound_but_denied_by_the_gate_does_not_launch(
        tmp_path, monkeypatch, stage_list):
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.80, 2, NOW), wk(0.40, 30, now=NOW)], implement=stage_list)
    assert launched(sess) == [LUNA_ID]


@BOTH_ORDERS
def test_no_session_week_share_keeps_the_required_pace_order(
        tmp_path, monkeypatch, stage_list):
    # The key is deliberately absent: PaceConfig's own default applies.
    c = PaceConfig(weekend_weight=1.0, pace_margin=1.0)
    sess, _ = run(tmp_path, monkeypatch,
                  both([ses(0.0, 5, NOW), wk(0.70, 30, now=NOW)], OPENAI_5_6()),
                  implement=stage_list, pace=c, frozen=NOW)
    assert launched(sess) == [LUNA_ID]


def test_session_bound_provider_still_yields_the_review_to_the_other_provider(
        tmp_path, monkeypatch):
    sess, _ = go(tmp_path, monkeypatch,
                 [ses(0.0, 5, NOW), wk(0.70, 30, now=NOW)],
                 implement=WRITTEN, review=[OPUS, SOL], stage=Stage.IMPLEMENT,
                 ticket_cursor=1, ticket_count=1, picks={"implement": SONNET})
    assert launched(sess) == ["openai/gpt-6-sol"]


# --- session_bound ---------------------------------------------------------

def test_session_bound_is_false_for_unavailable_usage_and_for_no_unscoped_weekly():
    from dispatcher.usage import (ProviderUsage, session_bound,
                                  unavailable)
    cfg = pace()
    # positive control: the first criterion's usage is bound
    assert session_bound(usage("anthropic", ses(0.0, 5, NOW),
                               wk(0.70, 30, now=NOW)), NOW, cfg) is True
    assert session_bound(unavailable("anthropic", 0.0), NOW, cfg) is False
    assert session_bound(usage("anthropic", ses(0.0, 5, NOW)), NOW, cfg) is False
    assert session_bound(usage("anthropic", ses(0.0, 5, NOW),
                               wk(0.10, 30, "Fable", NOW)), NOW, cfg) is False
    assert session_bound(ProviderUsage("anthropic", "oauth", 0.0, ()),
                         NOW, cfg) is False


# --- config ----------------------------------------------------------------

def _load(tmp_path, value):
    p = tmp_path / "targets.yaml"
    p.write_text(SAMPLE + f"session_week_share: {value}\n")
    return load_config(p)


@pytest.mark.parametrize("value", ["{anthropic: 0}", "{anthropic: 1.5}"])
def test_session_week_share_outside_0_to_1_fails_config_load(tmp_path, value):
    with pytest.raises(ValueError, match="session_week_share"):
        _load(tmp_path, value)


@pytest.mark.parametrize("value,expected", [("{anthropic: 1}", 1),
                                            ("{anthropic: 0.05}", 0.05)])
def test_session_week_share_1_and_0_05_load(tmp_path, value, expected):
    assert dict(_load(tmp_path, value).pace.session_week_share) == {
        "anthropic": expected}


# --- the gate is untouched -------------------------------------------------

@pytest.mark.parametrize("model,admitted,reason,kind,scope,allow,headroom", [
    ("claude-sonnet-5-5", True, "ok", "session", None, 0.8, 0.3),
    ("claude-fable-5-1", False, "over-pace", "weekly", "Fable", 0.68333, -0.21667),
    ("openai/gpt-6-sol", False, "over-threshold", "session", None, 0.8, -0.05),
])
def test_gate_verdict_is_identical_with_and_without_session_week_share(
        model, admitted, reason, kind, scope, allow, headroom):
    with_key = admits(_gate_usages(), model, GATE_NOW,
                      PaceConfig(session_week_share={"anthropic": 0.05}))
    without = admits(_gate_usages(), model, GATE_NOW, PaceConfig())
    assert with_key == without
    assert (with_key.admitted, with_key.reason) == (admitted, reason)
    b = with_key.binding
    assert (b.window.kind.value, b.window.scope) == (kind, scope)
    assert b.allowance == pytest.approx(allow, abs=1e-4)
    assert b.headroom == pytest.approx(headroom, abs=1e-4)
