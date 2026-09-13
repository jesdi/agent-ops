from datetime import datetime, timedelta, timezone

import pytest

from dispatcher import usage
from dispatcher.usage import (PaceConfig, ProviderUsage, Window, WindowKind,
                              admits, allowance, minutes_to_reset, readings,
                              verdict_note, weighted_hours, window_label)

SESSION, WEEKLY = WindowKind.SESSION, WindowKind.WEEKLY


def test_unavailable_has_no_windows():
    u = usage.unavailable("anthropic", 5.0)
    assert (u.source, u.windows, u.fetched_at) == ("unavailable", (), 5.0)


def test_window_kind_owns_its_length_and_deny_reason():
    assert (SESSION.length, WEEKLY.length) == (timedelta(hours=5), timedelta(days=7))
    assert (SESSION.deny_reason, WEEKLY.deny_reason) == ("over-threshold", "over-pace")
    assert Window(WEEKLY, None, 0.1, utc(2026, 9, 18)).length == timedelta(days=7)


PACE = PaceConfig(budget_threshold=0.8, racing_minutes=30, racing_threshold=0.95,
                  pace_margin=0.10, weekend_weight=0.5, timezone="Europe/Madrid")
RESET = datetime(2026, 9, 18, 12, 59, 59, tzinfo=timezone.utc)   # Fri 14:59:59 CEST
WEEK_WINDOW = Window(WEEKLY, None, 0.13, RESET)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, elapsed", [
    (utc(2026, 9, 12, 11, 0), 0.1076),   # Sat 13:00 CEST
    (utc(2026, 9, 13, 10, 30), 0.1892),  # Sun 12:30 CEST
    (utc(2026, 9, 14, 11, 0), 0.3194),   # Mon 13:00 CEST
    (utc(2026, 9, 16, 11, 0), 0.6528),   # Wed 13:00 CEST
])
def test_weekly_allowance_is_weighted_elapsed_plus_margin(now, elapsed):
    assert allowance(WEEK_WINDOW, now, PACE) == pytest.approx(elapsed + 0.10, abs=1e-3)


def test_weekly_allowance_caps_at_one_and_relaxes_at_the_end():
    assert allowance(WEEK_WINDOW, RESET, PACE) == 1.0
    assert allowance(WEEK_WINDOW, RESET - timedelta(hours=1), PACE) == 1.0


def test_weekend_weight_one_is_linear_time():
    lin = PaceConfig(**{**PACE.__dict__, "weekend_weight": 1.0})
    now = utc(2026, 9, 12, 11, 0)                      # 22h 0m 1s into 168h
    assert allowance(WEEK_WINDOW, now, lin) == pytest.approx(22.0003 / 168 + 0.10, abs=1e-3)


def test_weighted_hours_sums_the_window_to_144_for_half_weight_weekends():
    assert weighted_hours(RESET - WEEKLY.length, RESET, "Europe/Madrid", 0.5) == pytest.approx(144.0, abs=1e-6)


def test_weighted_hours_respects_dst_day_length():
    # Europe/Madrid falls back on Sun 2026-10-25: that Sunday has 25 local hours.
    start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)   # Sun 00:00 CEST
    end = datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)     # Mon 00:00 CET
    assert weighted_hours(start, end, "Europe/Madrid", 0.5) == pytest.approx(12.5)
    assert weighted_hours(start, end, "Europe/Madrid", 1.0) == pytest.approx(25.0)


def test_session_allowance_keeps_threshold_and_reset_racing():
    far = Window(SESSION, None, 0.5, utc(2026, 9, 13, 14, 0))
    soon = Window(SESSION, None, 0.5, utc(2026, 9, 13, 11, 20))
    now = utc(2026, 9, 13, 11, 0)
    assert allowance(far, now, PACE) == 0.8
    assert allowance(soon, now, PACE) == 0.95


def test_minutes_to_reset_floors_at_zero():
    w = Window(SESSION, None, 0.5, utc(2026, 9, 13, 11, 0))
    assert minutes_to_reset(w, utc(2026, 9, 13, 10, 30)) == 30
    assert minutes_to_reset(w, utc(2026, 9, 13, 12, 0)) == 0


NOW = utc(2026, 9, 13, 10, 30)          # Sun 12:30 CEST -> weekly allowance 0.289


def anthropic(session=0.05, week=0.13, fable=0.23, source="oauth"):
    ws = [Window(SESSION, None, session, NOW + timedelta(minutes=89)),
          Window(WEEKLY, None, week, RESET)]
    if fable is not None:
        ws.append(Window(WEEKLY, "Fable", fable, RESET))
    return {"anthropic": ProviderUsage("anthropic", source, 0.0, tuple(ws))}


def test_readings_judge_every_window_once():
    u = anthropic()["anthropic"]
    rs = readings(u, NOW, PACE)
    assert [r.window for r in rs] == list(u.windows)
    for r in rs:
        assert r.allowance == allowance(r.window, NOW, PACE)
        assert r.headroom == pytest.approx(r.allowance - r.window.used)


def test_an_unavailable_provider_has_no_readings():
    assert readings(anthropic(source="unavailable")["anthropic"], NOW, PACE) == ()


def test_scoped_window_applies_only_to_matching_models():
    u = anthropic(fable=0.28)            # Fable has the least headroom of all three
    assert admits(u, "claude-fable-5-1", NOW, PACE).binding.window.scope == "Fable"
    assert admits(u, "claude-sonnet-4-6", NOW, PACE).binding.window.scope is None


def test_fable_over_pace_blocks_fable_and_admits_sonnet():
    u = anthropic(fable=0.35)
    fable = admits(u, "anthropic/claude-fable-5-1", NOW, PACE)
    sonnet = admits(u, "claude-sonnet-4-6", NOW, PACE)
    assert (fable.admitted, fable.reason, fable.binding.window.scope) == (False, "over-pace", "Fable")
    assert fable.binding.headroom == pytest.approx(0.289 - 0.35, abs=1e-3)
    assert sonnet.admitted and sonnet.reason == "ok"


def test_binding_window_is_the_least_headroom():
    v = admits(anthropic(), "claude-fable-5-1", NOW, PACE)
    assert v.admitted and v.binding.window.scope == "Fable"
    assert v.binding.headroom == pytest.approx(0.289 - 0.23, abs=1e-3)
    assert v.binding.allowance == pytest.approx(0.289, abs=1e-3)


def test_weekly_all_over_pace_blocks_every_model():
    u = anthropic(week=0.40)
    assert not admits(u, "claude-sonnet-4-6", NOW, PACE).admitted
    assert not admits(u, "claude-fable-5-1", NOW, PACE).admitted


def test_session_over_threshold_reports_over_threshold():
    v = admits(anthropic(session=0.85), "claude-sonnet-4-6", NOW, PACE)
    assert (v.admitted, v.reason, v.binding.window.kind) == (False, "over-threshold", SESSION)


def test_unavailable_or_unknown_provider_fails_closed():
    dark = admits(anthropic(source="unavailable"), "claude-sonnet-4-6", NOW, PACE)
    missing = admits(anthropic(), "openai/gpt-5.4-codex", NOW, PACE)
    assert (dark.admitted, dark.reason, dark.binding) == (False, "unavailable", None)
    assert (missing.admitted, missing.reason, missing.provider) == (False, "unavailable", "openai")


def test_absent_window_passes():
    only_session = {"anthropic": ProviderUsage("anthropic", "ccusage", 0.0, (
        Window(SESSION, None, 0.5, NOW + timedelta(minutes=100)),))}
    assert admits(only_session, "claude-fable-5-1", NOW, PACE).admitted


def test_no_applicable_window_admits_without_a_binding():
    only_fable = {"anthropic": ProviderUsage("anthropic", "oauth", 0.0, (
        Window(WEEKLY, "Fable", 0.99, RESET),))}
    v = admits(only_fable, "claude-sonnet-4-6", NOW, PACE)
    assert (v.admitted, v.binding, v.reason) == (True, None, "ok")
    assert verdict_note(v, NOW) == "anthropic: no usage windows reported"


def test_another_provider_spends_its_own_windows():
    u = {**anthropic(week=0.9),
         "fake": ProviderUsage("fake", "oauth", 0.0, (
             Window(WEEKLY, None, 0.1, RESET),))}
    assert not admits(u, "claude-sonnet-4-6", NOW, PACE).admitted
    assert admits(u, "fake/any-model", NOW, PACE).admitted


# The pre-existing session truth table (tests/test_budget_policy.py), through admits.
@pytest.mark.parametrize("util, mins, source, expected", [
    (0.5, 200, "oauth", True), (0.85, 200, "oauth", False),
    (0.9, 20, "oauth", True), (0.97, 20, "oauth", False),
    (0.1, 200, "unavailable", False), (0.5, 200, "ccusage", True),
])
def test_session_truth_table(util, mins, source, expected):
    u = {"anthropic": ProviderUsage("anthropic", source, 0.0, (
        Window(SESSION, None, util, NOW + timedelta(minutes=mins)),))}
    assert admits(u, "claude-sonnet-4-6", NOW, PACE).admitted is expected


def test_window_label_and_note():
    v = admits(anthropic(fable=0.35), "claude-fable-5-1", NOW, PACE)
    assert window_label(v.binding.window) == "weekly·Fable"
    assert verdict_note(v, NOW) == (
        "anthropic weekly·Fable: 35% used, allowance 29%, headroom -6 pts, resets in 5d 2h")
    dark = admits(anthropic(source="unavailable"), "claude-sonnet-4-6", NOW, PACE)
    assert verdict_note(dark, NOW) == "anthropic: usage unavailable"
