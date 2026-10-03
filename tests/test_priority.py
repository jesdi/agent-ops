"""dispatcher.priority.order: the order a list's entries are tried in."""
import time
from datetime import datetime, timedelta, timezone

from dispatcher import priority
from dispatcher.models import candidates, parse_entry, parse_policy
from dispatcher.usage import PaceConfig, ProviderUsage, Window, WindowKind

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PACE = PaceConfig(weekend_weight=1.0)
SONNET, FABLE, LUNA = (parse_entry(e, "t") for e in (
    "claude-sonnet-5-5", "claude-fable-5-1", "openai/gpt-6-luna"))


def week(used, hours=84.0, scope=None):
    return Window(WindowKind.WEEKLY, scope, used, NOW + timedelta(hours=hours))


def usages(anthropic, openai):
    return {p: ProviderUsage(p, "oauth", time.time(), tuple(ws))
            for p, ws in (("anthropic", anthropic), ("openai", openai))}


def order(u):
    return priority.order(priority.AUTO, u, NOW, PACE)


def test_an_empty_reading_keeps_the_written_order():
    assert order({})([LUNA, SONNET]) == (LUNA, SONNET)


def test_highest_required_pace_first_and_unrated_entries_last():
    u = usages([week(0.5)], [week(0.2)])                      # 1.0x, 1.6x
    assert order(u)([SONNET, LUNA]) == (LUNA, SONNET)
    del u["anthropic"]
    assert order(u)([SONNET, LUNA]) == (LUNA, SONNET)


def test_paces_equal_at_one_decimal_keep_the_written_order():
    u = usages([week(0.50)], [week(0.48)])                    # 1.00x, 1.04x
    assert order(u)([SONNET, LUNA]) == (SONNET, LUNA)


def test_an_entry_ranks_on_the_lowest_window_its_model_draws_on():
    u = usages([week(0.2), week(0.9, scope="Fable")], [week(0.5)])
    assert order(u)([FABLE, LUNA, SONNET]) == (SONNET, LUNA, FABLE)


def test_candidates_apply_the_order_before_the_review_reorder():
    policy = parse_policy({"triage": ["claude-sonnet-5-5"], "untracked": "s", "tracks": {
        "s": {"when": "x", **{st: ["claude-sonnet-5-5", "openai/gpt-6-luna"]
                              for st in ("spec", "plan", "implement", "review")}}}})
    first = order(usages([week(0.5)], [week(0.2)]))
    assert candidates(policy, "s", "review", order=first) == (LUNA, SONNET)
    assert candidates(policy, "s", "review", "openai", first) == (SONNET, LUNA)
