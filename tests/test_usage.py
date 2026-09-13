import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dispatcher import usage
from dispatcher.usage import (SESSION, WEEK, ProviderUsage, Window,
                              parse_anthropic, usage_from_json, usage_to_json)

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
