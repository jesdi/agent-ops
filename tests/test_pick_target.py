"""The fair claim round's pick: fewest active wins, ties to the target that
claimed least recently (never claimed counts as oldest), then list order."""
from types import SimpleNamespace as T

from dispatcher.main import _pick_target

A, B = T(name="a", max_active=None), T(name="b", max_active=None)


def test_fewest_active_wins():
    assert _pick_target([A, B], {"a": 2, "b": 0}, {}) is B


def test_tie_goes_to_least_recently_claimed():
    last = {"a": "2026-07-21T09:00:00+00:00", "b": "2026-07-21T08:00:00+00:00"}
    assert _pick_target([A, B], {"a": 0, "b": 0}, last) is B


def test_never_claimed_counts_as_oldest():
    assert _pick_target([A, B], {"a": 0, "b": 0}, {"a": "2026-07-21T09:00:00+00:00"}) is B


def test_full_tie_goes_to_list_order():
    assert _pick_target([A, B], {"a": 1, "b": 1}, {}) is A


def test_target_at_max_active_is_skipped():
    capped = T(name="a", max_active=2)
    assert _pick_target([capped, B], {"a": 2, "b": 5}, {}) is B
    assert _pick_target([capped], {"a": 2}, {}) is None
