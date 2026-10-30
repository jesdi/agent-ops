"""dispatcher.claims: the one pick the dispatcher's claim round and the
console's next-claim forecast share, and the last-claims reader behind its
tie-break."""
import json

from dispatcher import eventlog
from dispatcher.claims import (last_claims, pick_project, project_counts,
                               project_last_claims)
from dispatcher.state import Stage
from tests.webfakes import make_task

NONE = {"a": None, "b": None}


def test_fewest_active_wins():
    assert pick_project(["a", "b"], {"a": 2, "b": 0}, {}, NONE) == "b"


def test_tie_goes_to_least_recently_claimed():
    last = {"a": "2026-07-21T09:00:00+00:00", "b": "2026-07-21T08:00:00+00:00"}
    assert pick_project(["a", "b"], {"a": 0, "b": 0}, last, NONE) == "b"


def test_never_claimed_counts_as_oldest():
    last = {"a": "2026-07-21T09:00:00+00:00"}
    assert pick_project(["a", "b"], {"a": 0, "b": 0}, last, NONE) == "b"


def test_full_tie_goes_to_list_order():
    assert pick_project(["a", "b"], {"a": 1, "b": 1}, {}, NONE) == "a"


def test_project_at_max_active_is_skipped():
    caps = {"a": 2, "b": None}
    assert pick_project(["a", "b"], {"a": 2, "b": 5}, {}, caps) == "b"
    assert pick_project(["a"], {"a": 2}, {}, caps) is None


def test_last_claims_keeps_latest_claimed_ts_per_repository(tmp_path):
    p = tmp_path / "events.jsonl"
    rows = [
        {"ts": "2026-07-21T09:00:00+00:00", "event": "claimed", "repository": "a"},
        {"ts": "2026-07-21T10:00:00+00:00", "event": "parked", "repository": "a"},
        {"ts": "2026-07-21T08:00:00+00:00", "event": "claimed", "repository": "a"},
        {"ts": "2026-07-21T07:00:00+00:00", "event": "claimed", "repository": "b"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n{torn\n")
    assert last_claims(tmp_path) == {"a": "2026-07-21T09:00:00+00:00",
                                     "b": "2026-07-21T07:00:00+00:00"}


def test_last_claims_empty_without_log(tmp_path):
    assert last_claims(tmp_path) == {}


def test_last_claims_reads_what_append_event_writes(tmp_path):
    eventlog.append_event(tmp_path, "claimed", repository="a", issue=1)
    assert set(last_claims(tmp_path)) == {"a"}


def test_last_claims_skips_rows_with_malformed_fields(tmp_path):
    (tmp_path / eventlog.EVENTS_FILE).write_text(
        '{"event": "claimed", "repository": "a", "ts": null}\n'
        '{"event": "claimed", "repository": 7, "ts": "2026-09-24T09:00:00+00:00"}\n'
        '{"event": "claimed", "repository": "a", "ts": "2026-09-24T08:00:00+00:00"}\n')
    assert last_claims(tmp_path) == {"a": "2026-09-24T08:00:00+00:00"}


def test_last_claims_skips_non_row_lines_that_mention_claimed(tmp_path):
    """Every line here contains the substring "claimed" (the cheap
    pre-filter) but none is a real claimed row: a truncated line, valid JSON
    that is not an object, and an object whose event is NOT claimed even
    though another field's value says "claimed"."""
    (tmp_path / eventlog.EVENTS_FILE).write_text(
        '{"event": "claimed", "repository": "a"\n'
        '["claimed"]\n'
        '{"event": "unclaimed", "note": "claimed"}\n'
        '{"event": "claimed", "repository": "a", "ts": "2026-09-24T08:00:00+00:00"}\n')
    assert last_claims(tmp_path) == {"a": "2026-09-24T08:00:00+00:00"}


def test_claim_room_counts_parked_and_pr_open_tasks_against_max_open():
    from dispatcher.claims import claim_room
    from dispatcher.state import NO_SLOT, PARK_REVIEW, Stage, TaskState

    def task(issue, stage, park=""):
        return TaskState(issue=issue, repository="a", stage=stage, slot=NO_SLOT,
                         worktree="", branch="", title="", updated_at="", park=park)

    tasks = [task(1, Stage.SPEC, PARK_REVIEW), task(2, Stage.PR_OPEN),
             task(3, Stage.DONE), task(4, Stage.CANCELED)]
    # none consumes capacity, two are unfinished
    assert claim_room(3, 10, tasks, triage_running=False) == 3
    assert claim_room(3, 2, tasks, triage_running=False) == 0
    assert claim_room(3, 3, tasks, triage_running=True) == 1


def test_project_counts_sum_active_tasks_over_a_projects_repositories():
    tasks = [make_task(issue=1, repository="a1", stage=Stage.IMPLEMENT),
             make_task(issue=1, repository="a2", stage=Stage.IMPLEMENT),
             make_task(issue=2, repository="gone", stage=Stage.IMPLEMENT),
             make_task(issue=3, repository="a1", stage=Stage.DONE)]
    project_of = {"a1": "a", "a2": "a", "b1": "b"}
    assert project_counts(["a", "b"], tasks, project_of) == {"a": 2, "b": 0}


def test_project_last_claims_take_the_latest_of_its_repositories():
    last = {"a1": "2026-07-21T08:00:00+00:00", "a2": "2026-07-21T09:00:00+00:00",
            "gone": "2026-07-21T10:00:00+00:00"}
    assert project_last_claims(last, {"a1": "a", "a2": "a"}) == {
        "a": "2026-07-21T09:00:00+00:00"}
