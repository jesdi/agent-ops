"""dispatcher.claims: the one pick the dispatcher's claim round and the
console's next-claim forecast share, and the last-claims reader behind its
tie-break."""
import json

from dispatcher import eventlog
from dispatcher.claims import last_claims, pick_target

NONE = {"a": None, "b": None}


def test_fewest_active_wins():
    assert pick_target(["a", "b"], {"a": 2, "b": 0}, {}, NONE) == "b"


def test_tie_goes_to_least_recently_claimed():
    last = {"a": "2026-07-21T09:00:00+00:00", "b": "2026-07-21T08:00:00+00:00"}
    assert pick_target(["a", "b"], {"a": 0, "b": 0}, last, NONE) == "b"


def test_never_claimed_counts_as_oldest():
    last = {"a": "2026-07-21T09:00:00+00:00"}
    assert pick_target(["a", "b"], {"a": 0, "b": 0}, last, NONE) == "b"


def test_full_tie_goes_to_list_order():
    assert pick_target(["a", "b"], {"a": 1, "b": 1}, {}, NONE) == "a"


def test_target_at_max_active_is_skipped():
    caps = {"a": 2, "b": None}
    assert pick_target(["a", "b"], {"a": 2, "b": 5}, {}, caps) == "b"
    assert pick_target(["a"], {"a": 2}, {}, caps) is None


def test_last_claims_keeps_latest_claimed_ts_per_target(tmp_path):
    p = tmp_path / "events.jsonl"
    rows = [
        {"ts": "2026-07-21T09:00:00+00:00", "event": "claimed", "target": "a"},
        {"ts": "2026-07-21T10:00:00+00:00", "event": "parked", "target": "a"},
        {"ts": "2026-07-21T08:00:00+00:00", "event": "claimed", "target": "a"},
        {"ts": "2026-07-21T07:00:00+00:00", "event": "claimed", "target": "b"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n{torn\n")
    assert last_claims(tmp_path) == {"a": "2026-07-21T09:00:00+00:00",
                                     "b": "2026-07-21T07:00:00+00:00"}


def test_last_claims_empty_without_log(tmp_path):
    assert last_claims(tmp_path) == {}


def test_last_claims_reads_what_append_event_writes(tmp_path):
    eventlog.append_event(tmp_path, "claimed", target="a", issue=1)
    assert set(last_claims(tmp_path)) == {"a"}


def test_last_claims_skips_rows_with_malformed_fields(tmp_path):
    (tmp_path / eventlog.EVENTS_FILE).write_text(
        '{"event": "claimed", "target": "a", "ts": null}\n'
        '{"event": "claimed", "target": 7, "ts": "2026-09-24T09:00:00+00:00"}\n'
        '{"event": "claimed", "target": "a", "ts": "2026-09-24T08:00:00+00:00"}\n')
    assert last_claims(tmp_path) == {"a": "2026-09-24T08:00:00+00:00"}


def test_last_claims_skips_non_row_lines_that_mention_claimed(tmp_path):
    """Every line here contains the substring "claimed" (the cheap
    pre-filter) but none is a real claimed row: a truncated line, valid JSON
    that is not an object, and an object whose event is NOT claimed even
    though another field's value says "claimed"."""
    (tmp_path / eventlog.EVENTS_FILE).write_text(
        '{"event": "claimed", "target": "a"\n'
        '["claimed"]\n'
        '{"event": "unclaimed", "note": "claimed"}\n'
        '{"event": "claimed", "target": "a", "ts": "2026-09-24T08:00:00+00:00"}\n')
    assert last_claims(tmp_path) == {"a": "2026-09-24T08:00:00+00:00"}
