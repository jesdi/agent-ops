import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatcher import usage
from dispatcher.usage import (SESSION, WEEK, ProviderUsage, Window,
                              parse_anthropic, usage_from_json, usage_to_json,
                              PaceConfig, allowance, minutes_to_reset, weighted_hours)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "anthropic-usage.json").read_text())


def test_parse_reads_three_windows_from_limits():
    ws = parse_anthropic(FIXTURE)
    assert [(w.kind, w.scope) for w in ws] == [
        ("session", None), ("weekly", None), ("weekly", "Fable")]
    assert [w.used for w in ws] == [0.05, 0.13, 0.23]
    assert [w.length for w in ws] == [SESSION, WEEK, WEEK]
    assert ws[0].resets_at == datetime(2026, 9, 13, 11, 59, 59, 776452, tzinfo=timezone.utc)
    assert ws[2].resets_at.tzinfo is not None


def test_parse_falls_back_to_named_windows_when_limits_absent():
    payload = {k: v for k, v in FIXTURE.items() if k != "limits"}
    ws = parse_anthropic(payload)
    assert [(w.kind, w.scope, w.used) for w in ws] == [
        ("session", None, 0.05), ("weekly", None, 0.13)]


def test_parse_locked_or_full_window_counts_as_fully_used():
    limits = [dict(FIXTURE["limits"][0], locked_reason="limit_reached"),
              dict(FIXTURE["limits"][1], percent=130)]
    ws = parse_anthropic({"limits": limits})
    assert [w.used for w in ws] == [1.0, 1.0]


def test_parse_skips_unknown_kinds_and_entries_without_reset():
    limits = [dict(FIXTURE["limits"][0], kind="monthly_mystery"),
              dict(FIXTURE["limits"][1], resets_at=None),
              FIXTURE["limits"][2]]
    ws = parse_anthropic({"limits": limits})
    assert [(w.kind, w.scope) for w in ws] == [("weekly", "Fable")]


def test_json_round_trip():
    u = ProviderUsage(provider="anthropic", source="oauth", fetched_at=1000.0,
                      windows=parse_anthropic(FIXTURE))
    assert usage_from_json(json.loads(json.dumps(usage_to_json(u)))) == u


def test_unavailable_has_no_windows():
    u = usage.unavailable("anthropic", 5.0)
    assert (u.source, u.windows, u.fetched_at) == ("unavailable", (), 5.0)


def test_parse_locked_without_percent_via_limits():
    # null percent + locked_reason in limits[] must return 1.0, not skip
    limits = [dict(FIXTURE["limits"][0], percent=None, locked_reason="limit_reached")]
    ws = parse_anthropic({"limits": limits})
    assert len(ws) == 1
    assert ws[0].used == 1.0


def test_parse_locked_without_percent_via_fallback():
    # null utilization + locked_reason in five_hour fallback must return 1.0, not skip
    payload = {
        "five_hour": {"utilization": None, "locked_reason": "limit_reached",
                      "resets_at": "2026-09-13T11:59:59.776452Z"},
        "seven_day": {"utilization": 10.0, "resets_at": "2026-09-20T00:00:00Z"}
    }
    ws = parse_anthropic(payload)
    assert len(ws) == 2
    assert ws[0].used == 1.0
    assert ws[1].used == 0.1


PACE = PaceConfig(budget_threshold=0.8, racing_minutes=30, racing_threshold=0.95,
                  pace_margin=0.10, weekend_weight=0.5, timezone="Europe/Madrid")
RESET = datetime(2026, 9, 18, 12, 59, 59, tzinfo=timezone.utc)   # Fri 14:59:59 CEST
WEEKLY = Window("weekly", None, 0.13, RESET, WEEK)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize("now, elapsed", [
    (utc(2026, 9, 12, 11, 0), 0.1076),   # Sat 13:00 CEST
    (utc(2026, 9, 13, 10, 30), 0.1892),  # Sun 12:30 CEST
    (utc(2026, 9, 14, 11, 0), 0.3194),   # Mon 13:00 CEST
    (utc(2026, 9, 16, 11, 0), 0.6528),   # Wed 13:00 CEST
])
def test_weekly_allowance_is_weighted_elapsed_plus_margin(now, elapsed):
    assert allowance(WEEKLY, now, PACE) == pytest.approx(elapsed + 0.10, abs=1e-3)


def test_weekly_allowance_caps_at_one_and_relaxes_at_the_end():
    assert allowance(WEEKLY, RESET, PACE) == 1.0
    assert allowance(WEEKLY, RESET - timedelta(hours=1), PACE) == 1.0


def test_weekend_weight_one_is_linear_time():
    lin = PaceConfig(**{**PACE.__dict__, "weekend_weight": 1.0})
    now = utc(2026, 9, 12, 11, 0)                      # 22h 0m 1s into 168h
    assert allowance(WEEKLY, now, lin) == pytest.approx(22.0003 / 168 + 0.10, abs=1e-3)


def test_weighted_hours_sums_the_window_to_144_for_half_weight_weekends():
    assert weighted_hours(RESET - WEEK, RESET, "Europe/Madrid", 0.5) == pytest.approx(144.0, abs=1e-6)


def test_weighted_hours_respects_dst_day_length():
    # Europe/Madrid falls back on Sun 2026-10-25: that Sunday has 25 local hours.
    start = datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)   # Sun 00:00 CEST
    end = datetime(2026, 10, 25, 23, 0, tzinfo=timezone.utc)     # Mon 00:00 CET
    assert weighted_hours(start, end, "Europe/Madrid", 0.5) == pytest.approx(12.5)
    assert weighted_hours(start, end, "Europe/Madrid", 1.0) == pytest.approx(25.0)


def test_session_allowance_keeps_threshold_and_reset_racing():
    far = Window("session", None, 0.5, utc(2026, 9, 13, 14, 0), SESSION)
    soon = Window("session", None, 0.5, utc(2026, 9, 13, 11, 20), SESSION)
    now = utc(2026, 9, 13, 11, 0)
    assert allowance(far, now, PACE) == 0.8
    assert allowance(soon, now, PACE) == 0.95


def test_minutes_to_reset_floors_at_zero():
    w = Window("session", None, 0.5, utc(2026, 9, 13, 11, 0), SESSION)
    assert minutes_to_reset(w, utc(2026, 9, 13, 10, 30)) == 30
    assert minutes_to_reset(w, utc(2026, 9, 13, 12, 0)) == 0


from dispatcher.usage import Verdict, admits, considered, verdict_note, window_label, judge

NOW = utc(2026, 9, 13, 10, 30)          # Sun 12:30 CEST -> weekly allowance 0.289


def anthropic(session=0.05, week=0.13, fable=0.23, source="oauth"):
    ws = [Window("session", None, session, NOW + timedelta(minutes=89), SESSION),
          Window("weekly", None, week, RESET, WEEK)]
    if fable is not None:
        ws.append(Window("weekly", "Fable", fable, RESET, WEEK))
    return {"anthropic": ProviderUsage("anthropic", source, 0.0, tuple(ws))}


def test_scoped_window_applies_only_to_matching_models():
    assert [w.scope for w in considered(anthropic()["anthropic"].windows, "claude-fable-5-1")] == [None, None, "Fable"]
    assert [w.scope for w in considered(anthropic()["anthropic"].windows, "claude-sonnet-4-6")] == [None, None]


def test_fable_over_pace_blocks_fable_and_admits_sonnet():
    u = anthropic(fable=0.35)
    fable = admits(u, "anthropic/claude-fable-5-1", NOW, PACE)
    sonnet = admits(u, "claude-sonnet-4-6", NOW, PACE)
    assert (fable.admitted, fable.reason, fable.window.scope) == (False, "over-pace", "Fable")
    assert fable.headroom == pytest.approx(0.289 - 0.35, abs=1e-3)
    assert sonnet.admitted and sonnet.reason == "ok"


def test_binding_window_is_the_least_headroom():
    v = admits(anthropic(), "claude-fable-5-1", NOW, PACE)
    assert v.admitted and v.window.scope == "Fable"
    assert v.headroom == pytest.approx(0.289 - 0.23, abs=1e-3)
    assert v.allowance == pytest.approx(0.289, abs=1e-3)


def test_weekly_all_over_pace_blocks_every_model():
    u = anthropic(week=0.40)
    assert not admits(u, "claude-sonnet-4-6", NOW, PACE).admitted
    assert not admits(u, "claude-fable-5-1", NOW, PACE).admitted


def test_session_over_threshold_reports_over_threshold():
    v = admits(anthropic(session=0.85), "claude-sonnet-4-6", NOW, PACE)
    assert (v.admitted, v.reason, v.window.kind) == (False, "over-threshold", "session")


def test_unavailable_or_unknown_provider_fails_closed():
    dark = admits(anthropic(source="unavailable"), "claude-sonnet-4-6", NOW, PACE)
    missing = admits(anthropic(), "openai/gpt-5.4-codex", NOW, PACE)
    assert (dark.admitted, dark.reason, dark.window) == (False, "unavailable", None)
    assert (missing.admitted, missing.reason, missing.provider) == (False, "unavailable", "openai")


def test_absent_window_passes():
    only_session = {"anthropic": ProviderUsage("anthropic", "ccusage", 0.0, (
        Window("session", None, 0.5, NOW + timedelta(minutes=100), SESSION),))}
    assert admits(only_session, "claude-fable-5-1", NOW, PACE).admitted


def test_another_provider_spends_its_own_windows():
    u = {**anthropic(week=0.9),
         "fake": ProviderUsage("fake", "oauth", 0.0, (
             Window("weekly", None, 0.1, RESET, WEEK),))}
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
        Window("session", None, util, NOW + timedelta(minutes=mins), SESSION),))}
    assert admits(u, "claude-sonnet-4-6", NOW, PACE).admitted is expected


def test_window_label_and_note():
    v = admits(anthropic(fable=0.35), "claude-fable-5-1", NOW, PACE)
    assert window_label(v.window) == "weekly·Fable"
    assert verdict_note(v, NOW) == (
        "anthropic weekly·Fable: 35% used, allowance 29%, headroom -6 pts, resets in 5d 2h")
    dark = admits(anthropic(source="unavailable"), "claude-sonnet-4-6", NOW, PACE)
    assert verdict_note(dark, NOW) == "anthropic: usage unavailable"


def test_judge_names_binding_window_across_all_provider_windows():
    """judge() over all windows (incl. Fable) names Fable as binding for Sonnet context.
    admits() for Sonnet would not consider the Fable window."""
    u = anthropic()["anthropic"]
    # Fable window has highest used (0.23) so least headroom among all three
    v = judge("anthropic", u, u.windows, NOW, PACE)
    assert v.admitted
    assert v.window is not None and v.window.scope == "Fable"
