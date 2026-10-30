"""Board rank order. Ported from the backlog skill's test_rank.py (0.1.8),
whose rank.py the dispatcher used to run as rank_cmd."""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

from dispatcher import ranking
from dispatcher.board import Item, _item

REPO = "jesdi/alpha"


def _item_(number, title=None, status="Ready", impact=None, effort=None,
           boost=None, repository=REPO):
    return Item(id=f"I{number}", number=number, title=title or f"i{number}",
                status=status, impact=impact, effort=effort, area=None,
                boost=boost, url=f"u/{number}", repository=repository)


def _redacted(title, status="Ready", impact=None, effort=None):
    """An item whose issue the project token cannot see: no number, url or
    repository, only the board's Title field."""
    return Item(id=f"I-{title}", number=None, title=title, status=status,
                impact=impact, effort=effort, area=None)


def _issue(number, body="", state="OPEN", labels=(), title=None, repository=REPO):
    return {"repository": repository, "number": number,
            "title": title or f"i{number}", "url": f"u/{number}", "body": body,
            "state": state, "labels": list(labels)}


def _rank(items, issues=None):
    """Rank `items` with an issue for each numbered item unless given."""
    if issues is None:
        issues = [_issue(i.number) for i in items if i.number is not None]
    return ranking.rank(items, issues)


def _order(rows):
    return [(r["number"], r["blocked"]) for r in rows]


def test_blocked_by_reads_every_line_case_insensitively():
    assert ranking.blocked_by("Blocked by: #12, #34") == [12, 34]
    body = "Some idea.\n\nblocked BY: #5\nMore text\nBlocked by: #5, #9"
    assert ranking.blocked_by(body) == [5, 9]  # deduped + sorted
    assert ranking.blocked_by(None) == []
    assert ranking.blocked_by("no blockers here") == []


def test_items_join_their_issues_body_state_and_labels():
    rows = _rank([_item_(12, impact=4, effort=2), _item_(34, status=None),
                  _redacted("a draft note")],  # draft: no issue -> skipped
                 [_issue(12, body="Blocked by: #34", state="open", labels=["auto"]),
                  _issue(34, state="open")])
    assert _order(rows) == [(34, False), (12, True)]
    assert rows[1] == {"number": 12, "title": "i12", "url": "u/12",
                       "status": "Ready", "labels": ["auto"], "blocked": True,
                       "score": 2.0, "boost": 0}
    assert rows[0]["status"] is None and rows[0]["score"] is None


def test_a_redacted_item_joins_its_issue_by_title():
    rows = ranking.rank([_redacted("Add X", impact=4, effort=2)], [
        _issue(12, title="Add X", body="Blocked by: #34", labels=["auto"])])
    assert rows == [{"number": 12, "title": "Add X", "url": "u/12",
                     "status": "Ready", "labels": ["auto"], "blocked": False,
                     "score": 2.0, "boost": 0}]


def test_a_redacted_item_without_a_matching_title_is_skipped():
    assert ranking.rank([_redacted("board-only note")],
                        [_issue(1, title="Other")]) == []


def test_a_draft_or_pull_request_never_joins_an_issue_by_title():
    """Only an issue the token cannot see joins by title: a draft card or a
    pull request that happens to share an issue's title is not that issue."""
    hidden = [Item(id=f"I-{kind}", number=None, title="Ship it", status="Ready",
                   impact=None, effort=None, area=None, kind=kind)
              for kind in ("DRAFT_ISSUE", "PULL_REQUEST")]
    assert ranking.rank(hidden, [_issue(1, title="Ship it")]) == []
    redacted = dc_replace(hidden[0], kind="REDACTED")
    assert _order(ranking.rank([redacted], [_issue(1, title="Ship it")])) == [(1, False)]


def test_the_title_join_skips_a_title_two_issues_share():
    assert ranking.rank([_redacted("Dup")],
                        [_issue(1, title="Dup"), _issue(2, title="Dup")]) == []


def test_an_item_without_an_issue_row_reads_open_with_no_body_or_labels():
    rows = _rank([_item_(7, impact=3, effort=1)], [])
    assert rows[0]["labels"] == [] and rows[0]["blocked"] is False


def test_a_closed_or_done_blocker_is_satisfied_and_any_other_is_not():
    items = [_item_(1), _item_(2), _item_(3, status="Done"), _item_(4)]
    issues = [_issue(1, body="Blocked by: #2, #3, #4"), _issue(2, state="CLOSED"),
              _issue(3), _issue(4)]
    assert _order(ranking.rank(items, issues)) == [(4, False), (1, True)]
    issues[3]["state"] = "CLOSED"
    assert _order(ranking.rank(items, issues)) == [(1, False)]


def test_a_blocker_off_the_board_is_a_dropped_edge():
    rows = _rank([_item_(1)], [_issue(1, body="Blocked by: #999, #5"), _issue(5)])
    assert _order(rows) == [(1, False)]


def test_score_is_impact_over_effort_with_whole_numbers():
    rows = _rank([_item_(1, impact=4, effort=2), _item_(2, impact=None, effort=2),
                  _item_(3, impact=4, effort=None), _item_(4, impact=4, effort=0),
                  _item_(5, impact=2.5, effort=1)])
    assert {r["number"]: r["score"] for r in rows} == {
        1: 2.0, 2: None, 3: None, 4: None, 5: 2.0}


def test_available_by_score_then_blocked_and_done_dropped():
    rows = _rank([_item_(1, impact=2, effort=2), _item_(2, impact=5, effort=1),
                  _item_(3), _item_(4, impact=9, effort=1),
                  _item_(5, impact=4, effort=1, status="Done")],
                 [_issue(1), _issue(2), _issue(3), _issue(4, body="Blocked by: #2"),
                  _issue(5)])
    assert _order(rows) == [(2, False), (1, False), (3, False), (4, True)]


def test_closed_issues_are_dropped():
    rows = _rank([_item_(1, impact=3, effort=1), _item_(2, impact=1, effort=1)],
                 [_issue(1, state="CLOSED"), _issue(2)])
    assert _order(rows) == [(2, False)]


def test_ties_break_on_impact_then_number():
    rows = _rank([_item_(1, impact=2, effort=2), _item_(2, impact=4, effort=4),
                  _item_(4, impact=1, effort=1), _item_(3, impact=1, effort=1)])
    assert [r["number"] for r in rows] == [2, 1, 3, 4]


def test_in_progress_comes_last_and_never_reads_blocked():
    rows = _rank([_item_(3, impact=3, effort=1, status="In progress"),
                  _item_(1, impact=3, effort=1),
                  _item_(2, impact=4, effort=1, status="In progress")],
                 [_issue(1), _issue(2, body="Blocked by: #1"), _issue(3)])
    assert _order(rows) == [(1, False), (2, False), (3, False)]


def test_an_in_progress_blocker_still_blocks_its_dependents():
    rows = _rank([_item_(1, impact=3, effort=1, status="In progress"),
                  _item_(2, impact=4, effort=1)],
                 [_issue(1), _issue(2, body="Blocked by: #1")])
    assert _order(rows) == [(2, True), (1, False)]


def test_boost_defaults_to_zero_and_keeps_its_sign():
    rows = _rank([_item_(7, boost=2), _item_(8), _item_(9, boost=-3.0)])
    assert [(r["number"], r["boost"]) for r in rows] == [(7, 2), (8, 0), (9, -3)]


def test_the_boost_band_dominates_score():
    rows = _rank([_item_(1, impact=5, effort=1), _item_(2, impact=1, effort=2, boost=1)])
    assert [r["number"] for r in rows] == [2, 1]


def test_score_orders_within_a_boost_band():
    rows = _rank([_item_(1, impact=2, effort=2, boost=1),
                  _item_(2, impact=5, effort=1, boost=1)])
    assert [r["number"] for r in rows] == [2, 1]


def test_a_negative_boost_sinks_below_unboosted():
    rows = _rank([_item_(1, impact=9, effort=1, boost=-1), _item_(2, impact=1, effort=1)])
    assert [r["number"] for r in rows] == [2, 1]


def test_an_unscored_boosted_issue_follows_the_scored_in_its_band():
    rows = _rank([_item_(3, impact=5, effort=1), _item_(2, boost=1),
                  _item_(1, impact=2, effort=1, boost=1)])
    assert [r["number"] for r in rows] == [1, 2, 3]


def test_the_same_number_in_two_repositories_joins_its_own_issue():
    """rank.py read one repository and keyed by number alone; here each item
    gets its own repository's body and labels, and `Blocked by: #n` names
    an issue of the same repository."""
    other = "jesdi/beta"
    rows = ranking.rank(
        [_item_(1), _item_(2), _item_(1, repository=other),
         _item_(2, repository=other, status="Done")],
        [_issue(1, body="Blocked by: #2", labels=["a"]), _issue(2),
         _issue(1, body="Blocked by: #2", labels=["b"], repository=other),
         _issue(2, repository=other)])
    assert [(r["number"], r["labels"], r["blocked"]) for r in rows] == [
        (1, ["b"], False), (2, [], False), (1, ["a"], True)]


def test_repositories_join_whatever_their_case():
    rows = _rank([_item_(1, repository="Jesdi/Alpha")], [_issue(1, labels=["auto"])])
    assert rows[0]["labels"] == ["auto"]


GOLDEN = Path(__file__).parent / "fixtures" / "rank-golden.json"


def test_the_order_matches_what_rank_py_printed_for_the_same_board():
    """A recorded portfolio-eval board (160 items) and issue list (162),
    read-only with gh, and the 52 rows rank.py 0.1.8 --json printed for them.
    The private repository's text is scrubbed without touching what ranking
    reads: titles map one-to-one to placeholders, bodies keep only their
    Blocked-by lines. With every item's issue hidden, as the project token
    sees a private repository, the title join gives the same rows."""
    golden = json.loads(GOLDEN.read_text())
    nodes = golden["board_nodes"]
    assert ranking.rank([_item(n) for n in nodes], golden["issues"]) == golden["rows"]
    hidden = [_item({**n, "content": None, "type": "REDACTED"}) for n in nodes]
    assert ranking.rank(hidden, golden["issues"]) == golden["rows"]
