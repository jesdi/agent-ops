"""The board: narrow GraphQL read, title join, scoring and stale-card repair."""
import json
import subprocess

import pytest

from dispatcher import board

def fields_reply(fields=None, project_id="PVT_1"):
    """The live board's fields query reply: name -> (id, option names or
    None), as GitHub answers it. The default is the board triage writes."""
    fields = FIELDS if fields is None else fields
    nodes = [{"id": fid, "name": name,
              **({} if opts is None else {"options": [
                  {"id": f"o_{o}", "name": o} for o in opts]})}
             for name, (fid, opts) in fields.items()]
    return json.dumps({"data": {"repositoryOwner": {"projectV2": {
        "id": project_id, "fields": {"nodes": nodes}}}}})


FIELDS = {
    "Title": ("F_TITLE", None),
    "Status": ("F_STATUS", ("Inbox", "Ready", "In progress", "Done", "Wont do")),
    "Impact": ("F_IMPACT", None), "Effort": ("F_EFFORT", None),
    "Score": ("F_SCORE", None), "Boost": ("F_BOOST", None),
    "Area": ("F_AREA", ("frontend", "infra")),
}
LIVE_FIELDS = fields_reply()


def _node(item_id, number, title="", status=None, impact=None, effort=None,
          area=None):
    return {"id": item_id,
            "content": {"number": number} if number is not None else None,
            "title": {"text": title},
            "status": {"name": status} if status else None,
            "area": {"name": area} if area else None,
            "impact": {"number": impact} if impact is not None else None,
            "effort": {"number": effort} if effort is not None else None}


def _page(nodes, cursor=None):
    return json.dumps({"data": {"node": {"items": {
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": nodes}}}})


class FakeGh:
    """subprocess.run stand-in for gh: board pages in order, one issue list,
    and every mutation recorded."""

    def __init__(self, pages, issues, fail_mutations=False,
                 fields=LIVE_FIELDS):
        self.pages, self.issues, self.fields = list(pages), issues, fields
        self.fail_mutations = fail_mutations
        self.calls, self.mutations = [], []

    def __call__(self, args, capture_output=True, text=True, timeout=120,
                 env=None):
        self.calls.append((args, env))
        query = next((a for a in args if a.startswith("query=")), "")
        out, rc = "", 0
        if "mutation" in query:
            self.mutations.append(args)
            rc = 1 if self.fail_mutations else 0
        elif "projectV2(number" in query:
            out = self.fields
        elif args[1:3] == ["api", "graphql"]:
            out = self.pages.pop(0)
        elif args[1:3] == ["issue", "list"]:
            out = json.dumps(self.issues)
        return subprocess.CompletedProcess(args, rc, out, "rate limited" if rc else "")


def _issue(number, title="t", state="OPEN", reason=None):
    return {"number": number, "title": title, "state": state,
            "stateReason": reason}


def _read(nodes, issues, **kw):
    gh = FakeGh([_page(nodes)], issues, **kw)
    return board.read("o/a", "o", 1, run=gh), gh


def test_fetch_items_follows_pagination_and_never_uses_item_list():
    calls = []
    pages = [_page([_node("I1", 1)], cursor="c1"), _page([_node("I2", 2)])]

    def gh(args):
        calls.append(args)
        return pages.pop(0)

    items = board.fetch_items("PVT_1", gh)
    assert [(i.id, i.number) for i in items] == [("I1", 1), ("I2", 2)]
    assert all(a[:2] == ["api", "graphql"] for a in calls)
    assert "cursor=c1" in calls[1] and "cursor=c1" not in calls[0]


def test_fetch_items_reads_what_ranking_needs():
    """Boost, the item type, and the linked issue's own title, url and repository; a
    redacted item keeps the board's Title field and has no url or repository."""
    visible = {**_node("I1", 7, title="Field title"), "boost": {"number": 2},
               "type": "ISSUE"}
    visible["content"] = {"number": 7, "title": "Issue title", "url": "u/7",
                          "repository": {"nameWithOwner": "jesdi/Alpha"}}
    items = board.fetch_items("PVT_1", lambda args: _page(
        [visible, _node("I2", None, title="Private task")]))
    assert [(i.title, i.boost, i.url, i.repository, i.kind) for i in items] == [
        ("Issue title", 2, "u/7", "jesdi/Alpha", "ISSUE"),
        ("Private task", None, "", "", None)]


def test_join_uses_the_title_when_the_token_cannot_see_the_issue():
    items = [board.Item("I1", None, "Private task", "Ready", 3, 1, "infra")]
    joined = board.join(items, [_issue(9, "Private task"), _issue(10, "Other")])
    assert joined[9].id == "I1" and 10 not in joined


def test_join_skips_a_title_two_issues_share():
    items = [board.Item("I1", None, "Dup", None, None, None, None)]
    assert board.join(items, [_issue(1, "Dup"), _issue(2, "Dup")]) == {}


def test_untriaged_is_open_issues_without_a_score_or_still_in_inbox():
    brd, _ = _read([
        _node("I1", 1),                                    # never touched
        _node("I2", 2, status="Inbox", impact=3, effort=1),  # scored, not promoted
        _node("I3", 3, status="Ready", impact=3, effort=1),  # triaged by a person
        _node("I4", 4, status="Ready"),                    # promoted, never scored
        _node("I5", 5),                                    # closed: not our work
    ], [_issue(1), _issue(2), _issue(3), _issue(4),
        _issue(5, state="CLOSED", reason="COMPLETED"), _issue(6)])
    assert brd.untriaged() == [1, 2, 4]
    assert brd.off_board() == [6]


def test_score_sets_the_five_fields_in_one_request_with_the_board_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    brd, gh = _read([_node("I1", 1)], [_issue(1)])
    brd.score(1, 3, 2, "frontend")
    (mutation,) = gh.mutations
    query = next(a for a in mutation if a.startswith("query="))
    assert "i=I1" in mutation and "p=PVT_1" in mutation
    for expected in ('fieldId:"F_IMPACT",value:{number:3}',
                     'fieldId:"F_EFFORT",value:{number:2}',
                     'fieldId:"F_SCORE",value:{number:1.5}',
                     'fieldId:"F_AREA",value:{singleSelectOptionId:"o_frontend"}',
                     'fieldId:"F_STATUS",value:{singleSelectOptionId:"o_Ready"}'):
        assert expected in query
    env = gh.calls[-1][1]
    assert env["GH_TOKEN"] == "classic-tok"


def test_the_live_fields_are_read_once_per_project():
    gh = FakeGh([_page([]), _page([])], [])
    board.read("o/a", "o", 1, run=gh)
    board.read("o/b", "o", 1, run=gh)  # a second repository of the same project
    field_reads = [a for a, _ in gh.calls
                   if any("projectV2(number" in x for x in a)]
    assert len(field_reads) == 1
    assert "owner=o" in field_reads[0] and "number=1" in field_reads[0]


def test_fields_resolve_ids_by_name_and_name_what_is_missing():
    f = board.fields("o", 1, lambda args: LIVE_FIELDS)
    assert (f.id, f.field("Boost"), f.option("Status", "Wont do")) == (
        "PVT_1", "F_BOOST", "o_Wont do")
    assert f.options("Area") == {"frontend": "o_frontend", "infra": "o_infra"}
    with pytest.raises(board.BoardError, match="project o/1: no field 'Effort2'"):
        f.field("Effort2")
    with pytest.raises(board.BoardError, match=(
            "project o/1: field 'Status' has no option 'Blocked'")):
        f.option("Status", "Blocked")


def test_a_missing_project_is_a_board_error():
    reply = json.dumps({"data": {"repositoryOwner": {"projectV2": None}}})
    with pytest.raises(board.BoardError, match="project o/9: not found"):
        board.fields("o", 9, lambda args: reply)


def test_score_without_a_field_it_writes_fails_loudly_and_writes_nothing():
    fields = {k: v for k, v in FIELDS.items() if k != "Score"}
    brd, gh = _read([_node("I1", 1)], [_issue(1)],
                    fields=fields_reply(fields))
    with pytest.raises(board.BoardError, match="no field 'Score'"):
        brd.score(1, 3, 1, "infra")
    assert gh.mutations == []


def test_repair_without_a_wont_do_option_reports_it():
    fields = {**FIELDS, "Status": ("F_STATUS", ("Ready", "Done"))}
    brd, gh = _read([_node("I2", 2, status="Ready")],
                    [_issue(2, state="CLOSED", reason="NOT_PLANNED")],
                    fields=fields_reply(fields))
    assert brd.repair() == [
        "#2 closed: board status NOT repaired (project o/1: field 'Status' "
        "has no option 'Wont do')"]
    assert gh.mutations == []


def test_score_never_overwrites_values_a_person_set():
    brd, gh = _read([_node("I3", 3, status="Ready", impact=5, effort=1)],
                    [_issue(3)])
    with pytest.raises(ValueError, match="not awaiting a score"):
        brd.score(3, 1, 5, "infra")
    assert gh.mutations == []


@pytest.mark.parametrize("impact,effort,area", [
    (0, 1, "infra"), (6, 1, "infra"), (3, 0, "infra"), (3.5, 1, "infra"),
    ("3", 1, "infra"), (True, 1, "infra"), (3, 1, "made-up"), (3, 1, None)])
def test_score_rejects_values_outside_the_scale(impact, effort, area):
    brd, gh = _read([_node("I1", 1)], [_issue(1)])
    with pytest.raises(ValueError):
        brd.score(1, impact, effort, area)
    assert gh.mutations == []


def test_score_reports_a_refused_write():
    brd, _ = _read([_node("I1", 1)], [_issue(1)], fail_mutations=True)
    with pytest.raises(board.BoardError, match="rate limited"):
        brd.score(1, 3, 1, "infra")


def test_repair_moves_closed_issues_off_open_statuses():
    brd, gh = _read([
        _node("I1", 1, status="In progress"),
        _node("I2", 2, status="Ready"),
        _node("I3", 3),
        _node("I4", 4, status="Done"),          # already right
        _node("I5", 5, status="Ready"),         # still open
    ], [_issue(1, state="CLOSED", reason="COMPLETED"),
        _issue(2, state="CLOSED", reason="NOT_PLANNED"),
        _issue(3, state="CLOSED", reason="COMPLETED"),
        _issue(4, state="CLOSED", reason="COMPLETED"), _issue(5)])
    assert brd.repair() == ["#1 closed: board status -> Done",
                            "#2 closed: board status -> Wont do",
                            "#3 closed: board status -> Done"]
    queries = [next(a for a in m if a.startswith("query=")) for m in gh.mutations]
    assert ["o_Done" in q for q in queries] == [True, False, True]
    assert "o_Wont do" in queries[1]


def test_repair_reports_a_refused_write_and_continues():
    brd, gh = _read([_node("I1", 1, status="Ready"),
                            _node("I2", 2, status="Ready")],
                    [_issue(1, state="CLOSED"), _issue(2, state="CLOSED")],
                    fail_mutations=True)
    lines = brd.repair()
    assert len(lines) == 2 and all("NOT repaired" in l for l in lines)
