import json
import re
import subprocess
from dataclasses import replace as dc_replace

import dispatcher.github as github
from dispatcher.config import Board, Project, Repository
from tests.configfakes import with_board

REPOSITORY = Repository(
    name="portfolio_eval", repo="jesdi/portfolio_eval", github_id=1,
    clone_path="/home/agent/repos/portfolio_eval",
    worktrees_path="/home/agent/repos/portfolio_eval.worktrees",
    setup_cmd="scripts/setup-worktree.sh", verify_cmd="make e2e-slot SLOT={slot}",
    project=Project(
        name="portfolio_eval",
        board=Board(
            number=1, owner="jesdi", status_field_id="F1",
            status_ready_option_id="READY", status_in_progress_option_id="INPROG")))
ISSUES = "https://github.com/jesdi/portfolio_eval/issues"


def _board(*items):
    """One board GraphQL page: (id, number) or (id, None, title) per item."""
    return json.dumps({"data": {"node": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": i[0],
                   "content": {"number": i[1]} if i[1] is not None else None,
                   "title": {"text": i[2] if len(i) > 2 else ""}}
                  for i in items]}}}})


def _rank_node(number, repo="jesdi/portfolio_eval", status="Ready", impact=None,
               effort=None, boost=None):
    """A board item as the board query returns it, its issue visible."""
    return {"id": f"ITEM{number}",
            "content": {"number": number, "title": f"T{number}",
                        "url": f"https://github.com/{repo}/issues/{number}",
                        "repository": {"nameWithOwner": repo}},
            "title": {"text": f"T{number}"},
            "status": {"name": status} if status else None,
            "impact": {"number": impact} if impact is not None else None,
            "effort": {"number": effort} if effort is not None else None,
            "boost": {"number": boost} if boost is not None else None}


def _issue_node(number, labels=(), body="", state="OPEN"):
    return {"number": number, "title": f"T{number}", "url": f"u/{number}",
            "body": body, "state": state,
            "labels": {"nodes": [{"name": name} for name in labels]}}


def _ranking_gh(nodes, issues, calls=None):
    """_run stand-in for rank_rows: the project node id, one board page, and
    `issues["owner/name"]` as that repository's issue pages (--slurp's list),
    a list of lists being several pages."""
    def fake_run(args, cwd=None, env=None):
        if calls is not None:
            calls.append((args, env))
        if args[1:3] == ["project", "view"]:
            return json.dumps({"id": "PROJ_NODE"})
        fields = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("query="))
        if "project" in fields:
            return json.dumps({"data": {"node": {"items": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}})
        pages = issues[f"{fields['owner']}/{fields['name']}"]
        if not pages or not isinstance(pages[0], list):
            pages = [pages]
        return json.dumps([{"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": page}}}}
            for page in pages])
    return fake_run


BOARD = [_rank_node(7, impact=4, effort=1), _rank_node(8, impact=3, effort=1),
         _rank_node(9, status="Inbox", impact=2, effort=1),
         _rank_node(10, impact=5, effort=1), _rank_node(11, impact=1, effort=1)]
BOARD_ISSUES = {"jesdi/portfolio_eval": [
    _issue_node(7, ["auto"]), _issue_node(8), _issue_node(9, ["auto"]),
    _issue_node(10, ["auto"], body="Blocked by: #8"),
    _issue_node(11, ["auto", "frontend"])]}


def test_candidates_are_ready_unblocked_auto_rows_in_rank_order(monkeypatch):
    monkeypatch.setattr(github, "_run", _ranking_gh(BOARD, BOARD_ISSUES))
    got = github.GitHubClient().candidates([REPOSITORY])
    assert [(c.number, c.labels, c.repository) for c in got] == [
        (7, ("auto",), "portfolio_eval"), (11, ("auto", "frontend"), "portfolio_eval")]
    assert all(c.effort is None for c in got)  # rows carry no effort, as before


def test_rank_rows_reads_the_board_with_the_project_token_and_issues_without(
        monkeypatch):
    """One board read per call, one issue read per member with the stored
    auth (the project token cannot see a private repository's issues), and
    the project node id looked up once per process."""
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    calls = []
    monkeypatch.setattr(github, "_run", _ranking_gh(BOARD, BOARD_ISSUES, calls))
    client = github.GitHubClient()
    rows = client.rank_rows([REPOSITORY])
    client.rank_rows([REPOSITORY])
    assert [(r["number"], r["blocked"]) for r in rows] == [
        (7, False), (8, False), (9, False), (11, False), (10, True)]
    assert rows[0] == {"number": 7, "title": "T7", "url": f"{ISSUES}/7",
                       "status": "Ready", "labels": ["auto"], "blocked": False,
                       "score": 4.0, "boost": 0, "repository": "portfolio_eval"}
    kinds = [("view" if a[1] == "project" else "board" if "project=PROJ_NODE" in a
              else "issues", env and env.get("GH_TOKEN")) for a, env in calls]
    assert kinds == [("view", "classic-tok"), ("board", "classic-tok"), ("issues", None),
                     ("board", "classic-tok"), ("issues", None)]
    issues_call = calls[2][0]
    assert "--paginate" in issues_call and "owner=jesdi" in issues_call
    assert "name=portfolio_eval" in issues_call


def test_rank_rows_maps_rows_to_members_by_url(monkeypatch):
    """Each row goes to the member its url names (any case); the same number
    in both members stays two rows, each with its own labels, and an item of a
    repository outside the project is dropped."""
    factorial = dc_replace(REPOSITORY, name="factorial", repo="jesdi/Factorial",
                           github_id=2, clone_path="/home/agent/repos/factorial")
    nodes = [_rank_node(7, repo="jesdi/factorial", impact=2, effort=1),
             _rank_node(5, repo="jesdi/elsewhere", impact=5, effort=1),
             _rank_node(7, impact=1, effort=1)]
    issues = {"jesdi/portfolio_eval": [_issue_node(7, ["pe"])],
              "jesdi/Factorial": [_issue_node(7, ["f"])]}
    calls = []
    monkeypatch.setattr(github, "_run", _ranking_gh(nodes, issues, calls))
    rows = github.GitHubClient().rank_rows([REPOSITORY, factorial])
    assert [(r["repository"], r["number"], r["labels"]) for r in rows] == [
        ("factorial", 7, ["f"]), ("portfolio_eval", 7, ["pe"])]
    reads = [next(a for a in args if a.startswith(("project=", "name=")))
             for args, _env in calls if args[1] == "api"]
    assert reads == ["project=PROJ_NODE", "name=portfolio_eval", "name=Factorial"]


def test_rank_rows_reads_every_issue_page(monkeypatch):
    nodes = [_rank_node(7, impact=1, effort=1), _rank_node(8, impact=2, effort=1)]
    issues = {"jesdi/portfolio_eval": [[_issue_node(7, ["a"])], [_issue_node(8, ["b"])]]}
    monkeypatch.setattr(github, "_run", _ranking_gh(nodes, issues))
    rows = github.GitHubClient().rank_rows([REPOSITORY])
    assert [(r["number"], r["labels"]) for r in rows] == [(8, ["b"]), (7, ["a"])]


def test_rank_rows_boost_is_an_int(monkeypatch):
    nodes = [_rank_node(7, boost=5.0), _rank_node(8)]
    issues = {"jesdi/portfolio_eval": [_issue_node(7), _issue_node(8)]}
    monkeypatch.setattr(github, "_run", _ranking_gh(nodes, issues))
    rows = github.GitHubClient().rank_rows([REPOSITORY])
    assert [(r["number"], r["boost"]) for r in rows] == [(7, 5), (8, 0)]
    assert all(type(r["boost"]) is int for r in rows)


def test_claim_sets_status_and_comments(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7), ("ITEM8", 8))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    c = github.Candidate(number=7, title="A", url="u7")
    github.GitHubClient().claim(REPOSITORY, c)
    edit = next(a for a in calls if "item-edit" in a)
    assert "ITEM7" in edit and "INPROG" in edit and "F1" in edit
    comment = next(a for a in calls if "comment" in a)
    assert "🤖 picked up by agent-ops" in " ".join(comment)


def test_release_comments_and_resets_board(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().release(REPOSITORY, 7, "session crashed")
    edit = next(a for a in calls if "item-edit" in a)
    assert "READY" in edit
    comment = next(a for a in calls if "comment" in a)
    assert "session crashed" in " ".join(comment)


def test_cancel_comments_moves_board_and_closes_issue(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    t = with_board(REPOSITORY, status_wont_do_option_id="WONTDO")
    github.GitHubClient().cancel(t, 7)
    edit = next(a for a in calls if "item-edit" in a)
    assert "WONTDO" in edit and "ITEM7" in edit
    close = next(a for a in calls if "close" in a)
    assert "--reason" in close and "not planned" in close
    comment = next(a for a in calls if "comment" in a)
    assert "canceled by operator" in " ".join(comment)


def test_cancel_without_board_option_still_closes_issue(monkeypatch, caplog):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    with caplog.at_level("WARNING", logger="dispatcher.github"):
        github.GitHubClient().cancel(REPOSITORY, 7)  # status_wont_do_option_id == ""
    assert not any("item-edit" in a for a in calls)
    assert any("close" in a for a in calls)
    # the skipped board write is a logging event, not a bare print
    assert any("status_wont_do_option_id unset" in r.message
               for r in caplog.records)


def test_item_id_title_join_when_content_redacted(monkeypatch):
    # A project-scope token cannot expand linked issues of a private repo:
    # the board read returns items without "content". The issue title (fetched with
    # the stored repo auth) is the join key — GitHub syncs linked-item titles.
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", None, "A"), ("ITEM8", None, "B"))
        if "issue view" in joined:
            assert "7" in args and env is None  # repo-side read, stored auth
            return json.dumps({"title": "A"})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().claim(REPOSITORY, github.Candidate(7, "A", "u7"))
    edit = next(a for a, _ in calls if "item-edit" in a)
    assert "ITEM7" in edit


def test_item_id_title_join_ambiguous_duplicate_titles_raises(monkeypatch):
    def fake_run(args, cwd=None, env=None):
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", None, "Dup"), ("ITEM9", None, "Dup"))
        if "issue view" in joined:
            return json.dumps({"title": "Dup"})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    import pytest
    with pytest.raises(LookupError):
        github.GitHubClient()._item_id(REPOSITORY, 7)


def test_run_status_running_is_empty(monkeypatch):
    import dispatcher.github as gh
    monkeypatch.setattr(gh, "_run",
                        lambda a, cwd=None: '{"status":"in_progress","conclusion":null}')
    assert github.GitHubClient().run_status(REPOSITORY, 4242) == ""


def test_run_status_completed_returns_conclusion(monkeypatch):
    import dispatcher.github as gh
    monkeypatch.setattr(gh, "_run",
                        lambda a, cwd=None: '{"status":"completed","conclusion":"failure"}')
    assert github.GitHubClient().run_status(REPOSITORY, 4242) == "failure"


def test_dry_run_mutates_nothing(monkeypatch, capsys):
    def fake_run(args, cwd=None):
        raise AssertionError(f"dry-run must not execute: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    cli = github.GitHubClient(dry_run=True)
    cli.claim(REPOSITORY, github.Candidate(7, "A", "u7"))
    cli.comment(REPOSITORY, 7, "hi")
    cli.set_status(REPOSITORY, 7, "READY")
    out = capsys.readouterr().out
    assert "[dry-run]" in out


def test_project_commands_use_project_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().claim(REPOSITORY, github.Candidate(7, "A", "u7"))
    for args, env in calls:
        if args[:2] in (["gh", "project"], ["gh", "api"]):
            assert env is not None and env["GH_TOKEN"] == "classic-tok"
        else:
            assert env is None


def test_project_commands_without_token_inherit_ambient_auth(monkeypatch):
    monkeypatch.delenv("GH_PROJECT_TOKEN", raising=False)
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().set_status(REPOSITORY, 7, "READY")
    assert calls and all(env is None for _, env in calls)


def test_create_issue_returns_number_from_url(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        return "https://github.com/jesdi/agent-ops/issues/501\n"

    monkeypatch.setattr(github, "_run", fake_run)
    n = github.GitHubClient().create_issue("jesdi/agent-ops", "boom", "body")
    assert n == 501
    args = calls[0]
    assert args[:3] == ["gh", "issue", "create"]
    assert "jesdi/agent-ops" in args and "boom" in args and "body" in args


def test_issue_view_parses_json(monkeypatch):
    payload = '{"title": "T", "body": "B", "url": "u"}'
    seen = []

    def _run(a, cwd=None, env=None):
        seen.append(a)
        return payload

    monkeypatch.setattr(github, "_run", _run)
    result = github.GitHubClient().issue_view("jesdi/alpha", 73)
    assert result == {"title": "T", "body": "B", "url": "u"}
    # The argv is the security-relevant surface: a swapped repo/number would
    # silently fetch a different issue's body, and a widened --json field list
    # would put unrequested issue data on the wire.
    assert seen == [["gh", "issue", "view", "73", "--repo", "jesdi/alpha",
                     "--json", "title,body,url"]]


def test_issue_state_parses_json(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda a, cwd=None, env=None: '{"state": "CLOSED"}')
    assert github.GitHubClient().issue_state("jesdi/agent-ops", 501) == "CLOSED"


BOOST_REPOSITORY = with_board(REPOSITORY, boost_field_id="FB")


def test_set_boost_edits_number_field_with_project_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().set_boost(BOOST_REPOSITORY, 7, -2)
    edit, env = next((a, e) for a, e in calls if "item-edit" in a)
    assert "ITEM7" in edit and "FB" in edit
    assert edit[edit.index("--number") + 1] == "-2"
    assert env["GH_TOKEN"] == "classic-tok"


def test_add_label_uses_repo_auth(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().add_label(REPOSITORY, 7, "auto")
    args, env = calls[0]
    assert args[:4] == ["gh", "issue", "edit", "7"]
    assert "--add-label" in args and "auto" in args
    assert env is None  # stored fine-grained auth, not the project token


def test_boost_dry_run_mutates_nothing(monkeypatch, capsys):
    def fake_run(args, cwd=None, env=None):
        raise AssertionError(f"dry-run must not execute: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    cli = github.GitHubClient(dry_run=True)
    cli.set_boost(BOOST_REPOSITORY, 7, 99)
    cli.add_label(REPOSITORY, 7, "auto")
    assert "[dry-run]" in capsys.readouterr().out


def test_create_issue_and_issue_state_dry_run(monkeypatch, capsys):
    def fake_run(args, cwd=None, env=None):
        raise AssertionError(f"dry-run must not execute: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    cli = github.GitHubClient(dry_run=True)
    assert cli.create_issue("jesdi/agent-ops", "t", "b") == 0
    assert cli.issue_state("jesdi/agent-ops", 501) == "OPEN"
    assert "[dry-run]" in capsys.readouterr().out


# rank.py's pattern, copied verbatim (portfolio_eval
# .claude/skills/backlog/rank.py) — the appended line must parse with it.
RANK_BLOCKED_RE = re.compile(r"^\s*Blocked by:\s*(.+)$", re.MULTILINE)


def _blocked_by_rig(monkeypatch, body):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        if "view" in args:
            return json.dumps({"body": body})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    return calls


def test_append_blocked_by_appends_parseable_line(monkeypatch):
    calls = _blocked_by_rig(monkeypatch, "Original body.")
    github.GitHubClient().append_blocked_by(REPOSITORY, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    m = RANK_BLOCKED_RE.search(new_body)
    assert m and "#77" in m.group(1)
    assert new_body.startswith("Original body.")


def test_append_blocked_by_idempotent(monkeypatch):
    calls = _blocked_by_rig(monkeypatch, "Body.\n\nBlocked by: #77\n")
    github.GitHubClient().append_blocked_by(REPOSITORY, 42, 77)
    assert not any("edit" in a for a in calls)


def test_append_blocked_by_idempotent_whatever_the_line_case(monkeypatch):
    # ranking reads `blocked by:` case-insensitively, so the edge is there.
    calls = _blocked_by_rig(monkeypatch, "Body.\n\nblocked BY: #5, #77\n")
    github.GitHubClient().append_blocked_by(REPOSITORY, 42, 77)
    assert not any("edit" in a for a in calls)


def test_append_blocked_by_distinct_blocker_appended(monkeypatch):
    # #7 referenced must not swallow #77, and a different blocker adds a line.
    calls = _blocked_by_rig(monkeypatch, "Body.\n\nBlocked by: #7\n")
    github.GitHubClient().append_blocked_by(REPOSITORY, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    assert len(RANK_BLOCKED_RE.findall(new_body)) == 2


def test_append_blocked_by_empty_body(monkeypatch):
    calls = _blocked_by_rig(monkeypatch, "")
    github.GitHubClient().append_blocked_by(REPOSITORY, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    assert RANK_BLOCKED_RE.search(new_body)


def test_append_blocked_by_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(
        github, "_run",
        lambda a, cwd=None, env=None: (_ for _ in ()).throw(AssertionError))
    github.GitHubClient(dry_run=True).append_blocked_by(REPOSITORY, 42, 77)
    assert "[dry-run]" in capsys.readouterr().out


def test_viewer_login_fetched_once_and_cached(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        return "agent-bot\n"

    monkeypatch.setattr(github, "_run", fake_run)
    gh = github.GitHubClient()
    assert gh.viewer_login() == "agent-bot"
    assert gh.viewer_login() == "agent-bot"
    assert len(calls) == 1
    assert calls[0] == ["gh", "api", "user", "--jq", ".login"]


def test_viewer_login_failure_reads_as_empty_not_cached(monkeypatch):
    def boom(args, cwd=None, env=None):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(github, "_run", boom)
    gh = github.GitHubClient()
    assert gh.viewer_login() == ""


def test_pr_view_fetches_expected_fields(monkeypatch):
    seen = []

    def fake_run(args, cwd=None, env=None):
        seen.append(args)
        return json.dumps({"state": "OPEN", "mergedAt": None,
                           "reviewDecision": "", "reviews": [], "comments": []})

    monkeypatch.setattr(github, "_run", fake_run)
    gh = github.GitHubClient()
    d = gh.pr_view(REPOSITORY, 12)
    assert d["state"] == "OPEN"
    assert seen[0] == ["gh", "pr", "view", "12", "--repo", REPOSITORY.repo,
                       "--json", "state,mergedAt,reviewDecision,reviews,comments,mergeable,headRefOid,headRefName"]


def test_pr_number_for_branch(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda args, cwd=None, env=None: json.dumps([{"number": 34}]))
    assert github.GitHubClient().pr_number_for_branch(REPOSITORY, "agent/task-7") == 34
    monkeypatch.setattr(github, "_run",
                        lambda args, cwd=None, env=None: "[]")
    assert github.GitHubClient().pr_number_for_branch(REPOSITORY, "agent/task-7") == 0


def test_pr_number_for_branch_prefers_the_open_pr(monkeypatch):
    """`gh pr list --state all` guarantees no ordering, so a branch with an
    earlier closed PR and a newer open one must not leave gh to decide which
    PR the dispatcher watches forever."""
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 12, "state": "CLOSED"}, {"number": 34, "state": "OPEN"}]))
    assert github.GitHubClient().pr_number_for_branch(REPOSITORY, "agent/task-7") == 34
    # Same rows in the opposite order resolve identically.
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 34, "state": "OPEN"}, {"number": 12, "state": "CLOSED"}]))
    assert github.GitHubClient().pr_number_for_branch(REPOSITORY, "agent/task-7") == 34


def test_pr_number_for_branch_falls_back_to_the_newest_closed(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 34, "state": "MERGED"}, {"number": 12, "state": "CLOSED"}]))
    assert github.GitHubClient().pr_number_for_branch(REPOSITORY, "agent/task-7") == 34


def test_delete_branch_swallows_gh_errors(monkeypatch):
    def boom(args, cwd=None, env=None):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(github, "_run", boom)
    github.GitHubClient().delete_branch(REPOSITORY, "agent/task-7")  # no raise


def test_delete_branch_dry_run_calls_nothing(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    github.GitHubClient(dry_run=True).delete_branch(REPOSITORY, "agent/task-7")


def workflow_run(*, id=10, workflow=1, number=1, attempt=1, event="pull_request",
                 head="abc", branch="agent/task-7", status="completed",
                 conclusion="failure", updated="2026-09-12T10:00:00Z"):
    return {"id": id, "workflow_id": workflow, "run_number": number,
            "run_attempt": attempt, "event": event, "head_sha": head,
            "head_branch": branch, "status": status, "conclusion": conclusion,
            "updated_at": updated}


def test_ci_reads_actions_and_statuses_with_current_head_and_pagination(monkeypatch):
    from dispatcher.pr_poll import CIStatus
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        if "actions/runs" in args[2]:
            return json.dumps([{"workflow_runs": [workflow_run()]},
                               {"workflow_runs": [workflow_run(id=11, workflow=2,
                                                             conclusion="timed_out")]}])
        return json.dumps([[{"id": 2, "context": "external-ci", "state": "error",
                             "created_at": "2026-09-12T11:00:00Z"}],
                           [{"id": 1, "context": "external-ci", "state": "success",
                             "created_at": "2026-09-12T09:00:00Z"}]])

    monkeypatch.setattr(github, "_run", fake_run)
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert got == [CIStatus("failure", "2026-09-12T10:00:00Z"),
                   CIStatus("timed_out", "2026-09-12T10:00:00Z"),
                   CIStatus("error", "2026-09-12T11:00:00Z")]
    assert calls == [
        ["gh", "api", f"repos/{REPOSITORY.repo}/actions/runs", "--method", "GET",
         "--paginate", "--slurp", "-f", "head_sha=abc", "-f", "per_page=100",
         "-f", "branch=agent/task-7"],
        ["gh", "api", f"repos/{REPOSITORY.repo}/commits/abc/statuses?per_page=100",
         "--paginate", "--slurp"]]


def test_newest_run_supersedes_red_but_other_workflows_and_events_survive(monkeypatch):
    from dispatcher.pr_poll import CIStatus
    runs = [workflow_run(id=12, number=2, conclusion="success"),
            workflow_run(id=10),  # older red run of same workflow/event
            workflow_run(id=13, event="push"),  # independent trigger
            workflow_run(id=14, workflow=2),  # independent workflow
            workflow_run(id=15, head="old-head"),
            workflow_run(id=16, branch="main")]
    monkeypatch.setattr(github, "_run", lambda args: json.dumps(
        [{"workflow_runs": runs}] if "actions/runs" in args[2] else [[]]))
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert got == [CIStatus("success", "2026-09-12T10:00:00Z"),
                   CIStatus("failure", "2026-09-12T10:00:00Z"),
                   CIStatus("failure", "2026-09-12T10:00:00Z")]


def test_running_rerun_supersedes_failed_attempt(monkeypatch):
    runs = [workflow_run(), workflow_run(attempt=2, status="in_progress",
                                         conclusion=None)]
    monkeypatch.setattr(github, "_run", lambda args: json.dumps(
        [{"workflow_runs": runs}] if "actions/runs" in args[2] else [[]]))
    assert github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7") == []


def test_new_failure_after_rerun_uses_new_timestamp(monkeypatch):
    from dispatcher.pr_poll import CIStatus, classify
    runs = [workflow_run(attempt=2, updated="2026-09-12T11:00:00Z"), workflow_run()]
    monkeypatch.setattr(github, "_run", lambda args: json.dumps(
        [{"workflow_runs": runs}] if "actions/runs" in args[2] else [[]]))
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert got == [CIStatus("failure", "2026-09-12T11:00:00Z")]
    assert classify({"state": "OPEN"}, "", "agent-bot", ci_statuses=got,
                    check_cursor="2026-09-12T10:00:00Z").kind == "check-failed"


def test_new_pending_commit_status_supersedes_old_failure(monkeypatch):
    from dispatcher.pr_poll import classify
    statuses = [[{"id": 5, "context": "external-ci", "state": "pending",
                  "created_at": "2026-09-12T11:00:00Z"}],
                [{"id": 4, "context": "external-ci", "state": "failure",
                  "created_at": "2026-09-12T10:00:00Z"}]]
    monkeypatch.setattr(github, "_run", lambda args: json.dumps(
        [{"workflow_runs": []}] if "actions/runs" in args[2] else statuses))
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert classify({"state": "OPEN"}, "", "agent-bot", ci_statuses=got).kind == "quiet"


def test_commit_status_denial_does_not_hide_failed_action(monkeypatch, caplog):
    from dispatcher.pr_poll import CIStatus

    def fake_run(args):
        if "actions/runs" in args[2]:
            return json.dumps([{"workflow_runs": [workflow_run()]}])
        raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: statuses denied")

    monkeypatch.setattr(github, "_run", fake_run)
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert got == [CIStatus("failure", "2026-09-12T10:00:00Z")]
    assert "statuses denied" in caplog.text and REPOSITORY.repo in caplog.text


def test_actions_denial_does_not_hide_failed_commit_status(monkeypatch, caplog):
    from dispatcher.pr_poll import CIStatus

    def fake_run(args):
        if "actions/runs" in args[2]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 403: actions denied")
        return json.dumps([[{"id": 1, "context": "external-ci", "state": "error",
                             "created_at": "2026-09-12T11:00:00Z"}]])

    monkeypatch.setattr(github, "_run", fake_run)
    got = github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7")
    assert got == [CIStatus("error", "2026-09-12T11:00:00Z")]
    assert "actions denied" in caplog.text


def test_missing_head_does_not_query_repository_wide_ci(monkeypatch):
    def unexpected(args):
        raise AssertionError(args)

    monkeypatch.setattr(github, "_run", unexpected)
    assert github.GitHubClient().ci_statuses(REPOSITORY, "", "agent/task-7") == []


def test_actions_timeout_does_not_discard_commit_statuses(monkeypatch, caplog):
    from dispatcher.pr_poll import CIStatus

    def fake_run(args):
        if "actions/runs" in args[2]:
            raise subprocess.TimeoutExpired(args, 120)
        return json.dumps([[{"id": 1, "context": "external-ci", "state": "failure",
                             "created_at": "2026-09-12T11:00:00Z"}]])

    monkeypatch.setattr(github, "_run", fake_run)
    assert github.GitHubClient().ci_statuses(REPOSITORY, "abc", "agent/task-7") == [
        CIStatus("failure", "2026-09-12T11:00:00Z")]
    assert "timed out" in caplog.text


def test_item_lookup_never_lists_the_whole_board_with_item_list(monkeypatch):
    """`gh project item-list` costs ~100 GraphQL points a call and every
    status write used to make one; the lookup is one narrow query now."""
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "api graphql" in joined:
            return _board(("ITEM7", 7))
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    assert github.GitHubClient()._item_id(REPOSITORY, 7) == "ITEM7"
    assert not any("item-list" in a for a in calls)
    query = next(a for a in calls if a[:3] == ["gh", "api", "graphql"])
    assert "project=PROJ_NODE" in query


def test_close_issue_closes_as_completed_and_dry_run_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "_run",
                        lambda args, cwd=None, env=None: calls.append(args) or "")
    github.GitHubClient().close_issue("o/r", 42)
    close = calls[0]
    assert "close" in close and "42" in close and "o/r" in close
    assert "--reason" in close and "completed" in close
    assert "not planned" not in close
    calls.clear()
    github.GitHubClient(dry_run=True).close_issue("o/r", 42)
    assert calls == []
