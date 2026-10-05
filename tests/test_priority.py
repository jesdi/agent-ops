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


def test_only_an_exact_tie_keeps_the_written_order():
    tie = order(usages([week(0.50)], [week(0.50)]))           # 1.00x, 1.00x
    assert tie([SONNET, LUNA]) == (SONNET, LUNA)
    assert tie([LUNA, SONNET]) == (LUNA, SONNET)
    u = usages([week(0.50)], [week(0.48)])                    # 1.00x, 1.04x
    assert order(u)([SONNET, LUNA]) == (LUNA, SONNET)


def test_a_window_without_a_pace_leaves_the_entry_ranked_on_the_one_that_has():
    # Unscoped reset passed 5 minutes ago (no pace); Fable's window is 1.6x.
    u = usages([week(0.5, hours=-5 / 60), week(0.2, scope="Fable")], [week(0.5)])
    assert order(u)([LUNA, SONNET, FABLE]) == (FABLE, LUNA, SONNET)


def test_an_entry_ranks_on_the_lowest_window_its_model_draws_on():
    u = usages([week(0.2), week(0.9, scope="Fable")], [week(0.5)])
    assert order(u)([FABLE, LUNA, SONNET]) == (SONNET, LUNA, FABLE)


def test_candidates_apply_the_order_before_the_review_reorder():
    policy = parse_policy({"triage": ["claude-sonnet-5-5"], "untracked": "s", "tracks": {
        "s": {"when": "x", **{st: ["claude-sonnet-5-5", "openai/gpt-6-luna"]
                              for st in ("spec", "plan", "implement", "review")}}}})
    first = order(usages([week(0.5)], [week(0.2)]))
    assert candidates(policy, "s", "review", order=first) == (LUNA, SONNET)
    assert candidates(policy, "s", "review", "openai", order=first) == (SONNET, LUNA)
