import json
import re
import subprocess
from dataclasses import replace as dc_replace

import dispatcher.github as github
from dispatcher.config import Target

TARGET = Target(
    name="portfolio_eval", repo="jesdi/portfolio_eval",
    clone_path="/home/agent/repos/portfolio_eval",
    worktrees_path="/home/agent/repos/portfolio_eval.worktrees",
    rank_cmd="python rank.py --json", setup_cmd="scripts/setup-worktree.sh",
    verify_cmd="make e2e-slot SLOT={slot}",
    project_number=1, project_owner="jesdi",
    status_field_id="F1", status_ready_option_id="READY",
    status_in_progress_option_id="INPROG",
)

RANKED = json.dumps([
    {"number": 7, "title": "A", "url": "u7", "labels": ["auto"], "status": "Ready",
     "blocked": False, "effort": 1},
    {"number": 8, "title": "B", "url": "u8", "labels": [], "status": "Ready",
     "blocked": False, "effort": 2},
    {"number": 9, "title": "C", "url": "u9", "labels": ["auto"], "status": "Backlog",
     "blocked": False, "effort": 1},
    {"number": 10, "title": "D", "url": "u10", "labels": ["auto"], "status": "Ready",
     "blocked": True, "effort": 1},
    {"number": 11, "title": "E", "url": "u11", "labels": ["auto", "frontend"],
     "status": "Ready", "blocked": False, "effort": 3},
])


def test_candidates_carry_effort_and_labels(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args, cwd=None: RANKED)
    got = {c.number: c for c in github.GitHubClient().candidates(TARGET)}
    assert got[7].effort == 1
    assert got[7].labels == ("auto",)
    assert got[11].effort == 3
    assert got[11].labels == ("auto", "frontend")


def test_candidates_tolerate_missing_effort(monkeypatch):
    unscored = json.dumps([{"number": 12, "title": "F", "url": "u12",
                            "labels": ["auto"], "status": "Ready", "blocked": False}])
    monkeypatch.setattr(github, "_run", lambda args, cwd=None: unscored)
    got = github.GitHubClient().candidates(TARGET)
    assert got[0].effort is None


def test_candidates_filters_and_keeps_rank_order(monkeypatch):
    calls = []

    def fake_run(args, cwd=None):
        calls.append((args, cwd))
        return RANKED

    monkeypatch.setattr(github, "_run", fake_run)
    got = github.GitHubClient().candidates(TARGET)
    assert [c.number for c in got] == [7, 11]
    assert calls[0][1] == TARGET.clone_path


def test_claim_sets_status_and_comments(monkeypatch):
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append(args)
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "item-list" in joined:
            return json.dumps({"items": [
                {"id": "ITEM7", "content": {"number": 7}},
                {"id": "ITEM8", "content": {"number": 8}},
            ]})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    c = github.Candidate(number=7, title="A", url="u7")
    github.GitHubClient().claim(TARGET, c)
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
        if "item-list" in joined:
            return json.dumps({"items": [{"id": "ITEM7", "content": {"number": 7}}]})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().release(TARGET, 7, "session crashed")
    edit = next(a for a in calls if "item-edit" in a)
    assert "READY" in edit
    comment = next(a for a in calls if "comment" in a)
    assert "session crashed" in " ".join(comment)


def test_item_id_title_join_when_content_redacted(monkeypatch):
    # A project-scope token cannot expand linked issues of a private repo:
    # item-list returns items without "content". The issue title (fetched with
    # the stored repo auth) is the join key — GitHub syncs linked-item titles.
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "item-list" in joined:
            return json.dumps({"items": [
                {"id": "ITEM7", "title": "A", "repository": ""},
                {"id": "ITEM8", "title": "B", "repository": ""},
            ]})
        if "issue view" in joined:
            assert "7" in args and env is None  # repo-side read, stored auth
            return json.dumps({"title": "A"})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().claim(TARGET, github.Candidate(7, "A", "u7"))
    edit = next(a for a, _ in calls if "item-edit" in a)
    assert "ITEM7" in edit


def test_item_id_title_join_ambiguous_duplicate_titles_raises(monkeypatch):
    def fake_run(args, cwd=None, env=None):
        joined = " ".join(args)
        if "item-list" in joined:
            return json.dumps({"items": [
                {"id": "ITEM7", "title": "Dup"},
                {"id": "ITEM9", "title": "Dup"},
            ]})
        if "issue view" in joined:
            return json.dumps({"title": "Dup"})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    import pytest
    with pytest.raises(LookupError):
        github.GitHubClient()._item_id(TARGET, 7)


def test_run_status_running_is_empty(monkeypatch):
    import dispatcher.github as gh
    monkeypatch.setattr(gh, "_run",
                        lambda a, cwd=None: '{"status":"in_progress","conclusion":null}')
    assert github.GitHubClient().run_status(TARGET, 4242) == ""


def test_run_status_completed_returns_conclusion(monkeypatch):
    import dispatcher.github as gh
    monkeypatch.setattr(gh, "_run",
                        lambda a, cwd=None: '{"status":"completed","conclusion":"failure"}')
    assert github.GitHubClient().run_status(TARGET, 4242) == "failure"


def test_dry_run_mutates_nothing(monkeypatch, capsys):
    def fake_run(args, cwd=None):
        raise AssertionError(f"dry-run must not execute: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    cli = github.GitHubClient(dry_run=True)
    cli.claim(TARGET, github.Candidate(7, "A", "u7"))
    cli.comment(TARGET, 7, "hi")
    cli.set_status(TARGET, 7, "READY")
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
        if "item-list" in joined:
            return json.dumps({"items": [{"id": "ITEM7", "content": {"number": 7}}]})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().claim(TARGET, github.Candidate(7, "A", "u7"))
    for args, env in calls:
        if args[:2] == ["gh", "project"]:
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
        if "item-list" in joined:
            return json.dumps({"items": [{"id": "ITEM7", "content": {"number": 7}}]})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().set_status(TARGET, 7, "READY")
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


def test_issue_state_parses_json(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda a, cwd=None, env=None: '{"state": "CLOSED"}')
    assert github.GitHubClient().issue_state("jesdi/agent-ops", 501) == "CLOSED"


BOOST_TARGET = dc_replace(TARGET, boost_field_id="FB")


def test_rank_rows_normalizes_missing_boost(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args, cwd=None: RANKED)
    rows = github.GitHubClient().rank_rows(TARGET)
    assert [r["number"] for r in rows] == [7, 8, 9, 10, 11]
    assert all(r["boost"] == 0 for r in rows)


def test_rank_rows_keeps_boost_when_present(monkeypatch):
    ranked = json.dumps([{"number": 7, "title": "A", "url": "u7",
                          "labels": ["auto"], "status": "Ready",
                          "blocked": False, "score": 2.0, "boost": 99}])
    monkeypatch.setattr(github, "_run", lambda args, cwd=None: ranked)
    assert github.GitHubClient().rank_rows(TARGET)[0]["boost"] == 99


def test_set_boost_edits_number_field_with_project_token(monkeypatch):
    monkeypatch.setenv("GH_PROJECT_TOKEN", "classic-tok")
    calls = []

    def fake_run(args, cwd=None, env=None):
        calls.append((args, env))
        joined = " ".join(args)
        if "project view" in joined:
            return json.dumps({"id": "PROJ_NODE"})
        if "item-list" in joined:
            return json.dumps({"items": [{"id": "ITEM7", "content": {"number": 7}}]})
        return ""

    monkeypatch.setattr(github, "_run", fake_run)
    github.GitHubClient().set_boost(BOOST_TARGET, 7, -2)
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
    github.GitHubClient().add_label(TARGET, 7, "auto")
    args, env = calls[0]
    assert args[:4] == ["gh", "issue", "edit", "7"]
    assert "--add-label" in args and "auto" in args
    assert env is None  # stored fine-grained auth, not the project token


def test_boost_dry_run_mutates_nothing(monkeypatch, capsys):
    def fake_run(args, cwd=None, env=None):
        raise AssertionError(f"dry-run must not execute: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    cli = github.GitHubClient(dry_run=True)
    cli.set_boost(BOOST_TARGET, 7, 99)
    cli.add_label(TARGET, 7, "auto")
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
    github.GitHubClient().append_blocked_by(TARGET, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    m = RANK_BLOCKED_RE.search(new_body)
    assert m and "#77" in m.group(1)
    assert new_body.startswith("Original body.")


def test_append_blocked_by_idempotent(monkeypatch):
    calls = _blocked_by_rig(monkeypatch, "Body.\n\nBlocked by: #77\n")
    github.GitHubClient().append_blocked_by(TARGET, 42, 77)
    assert not any("edit" in a for a in calls)


def test_append_blocked_by_distinct_blocker_appended(monkeypatch):
    # #7 referenced must not swallow #77, and a different blocker adds a line.
    calls = _blocked_by_rig(monkeypatch, "Body.\n\nBlocked by: #7\n")
    github.GitHubClient().append_blocked_by(TARGET, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    assert len(RANK_BLOCKED_RE.findall(new_body)) == 2


def test_append_blocked_by_empty_body(monkeypatch):
    calls = _blocked_by_rig(monkeypatch, "")
    github.GitHubClient().append_blocked_by(TARGET, 42, 77)
    edit = next(a for a in calls if "edit" in a)
    new_body = edit[edit.index("--body") + 1]
    assert RANK_BLOCKED_RE.search(new_body)


def test_append_blocked_by_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(
        github, "_run",
        lambda a, cwd=None, env=None: (_ for _ in ()).throw(AssertionError))
    github.GitHubClient(dry_run=True).append_blocked_by(TARGET, 42, 77)
    assert "[dry-run]" in capsys.readouterr().out


def test_rank_rows_casts_string_boost_to_int(monkeypatch):
    ranked = json.dumps([
        {"number": 7, "title": "A", "url": "u7", "labels": ["auto"],
         "status": "Ready", "blocked": False, "score": 2.0, "boost": "5"},
        {"number": 8, "title": "B", "url": "u8", "labels": ["auto"],
         "status": "Ready", "blocked": False, "score": 1.0, "boost": ""},
        {"number": 9, "title": "C", "url": "u9", "labels": ["auto"],
         "status": "Ready", "blocked": False, "score": 1.0, "boost": None},
    ])
    monkeypatch.setattr(github, "_run", lambda args, cwd=None: ranked)
    rows = github.GitHubClient().rank_rows(TARGET)
    assert [r["boost"] for r in rows] == [5, 0, 0]
    assert all(isinstance(r["boost"], int) for r in rows)


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
    d = gh.pr_view(TARGET, 12)
    assert d["state"] == "OPEN"
    assert seen[0] == ["gh", "pr", "view", "12", "--repo", TARGET.repo,
                       "--json", "state,mergedAt,reviewDecision,reviews,comments"]


def test_pr_number_for_branch(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda args, cwd=None, env=None: json.dumps([{"number": 34}]))
    assert github.GitHubClient().pr_number_for_branch(TARGET, "agent/task-7") == 34
    monkeypatch.setattr(github, "_run",
                        lambda args, cwd=None, env=None: "[]")
    assert github.GitHubClient().pr_number_for_branch(TARGET, "agent/task-7") == 0


def test_pr_number_for_branch_prefers_the_open_pr(monkeypatch):
    """`gh pr list --state all` guarantees no ordering, so a branch with an
    earlier closed PR and a newer open one must not leave gh to decide which
    PR the dispatcher watches forever."""
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 12, "state": "CLOSED"}, {"number": 34, "state": "OPEN"}]))
    assert github.GitHubClient().pr_number_for_branch(TARGET, "agent/task-7") == 34
    # Same rows in the opposite order resolve identically.
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 34, "state": "OPEN"}, {"number": 12, "state": "CLOSED"}]))
    assert github.GitHubClient().pr_number_for_branch(TARGET, "agent/task-7") == 34


def test_pr_number_for_branch_falls_back_to_the_newest_closed(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args, cwd=None, env=None: json.dumps(
        [{"number": 34, "state": "MERGED"}, {"number": 12, "state": "CLOSED"}]))
    assert github.GitHubClient().pr_number_for_branch(TARGET, "agent/task-7") == 34


def test_delete_branch_swallows_gh_errors(monkeypatch):
    def boom(args, cwd=None, env=None):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(github, "_run", boom)
    github.GitHubClient().delete_branch(TARGET, "agent/task-7")  # no raise


def test_delete_branch_dry_run_calls_nothing(monkeypatch):
    monkeypatch.setattr(github, "_run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    github.GitHubClient(dry_run=True).delete_branch(TARGET, "agent/task-7")
