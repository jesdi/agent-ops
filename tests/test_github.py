import json

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
    {"number": 7, "title": "A", "url": "u7", "labels": ["auto"], "status": "Ready", "blocked": False},
    {"number": 8, "title": "B", "url": "u8", "labels": [], "status": "Ready", "blocked": False},
    {"number": 9, "title": "C", "url": "u9", "labels": ["auto"], "status": "Backlog", "blocked": False},
    {"number": 10, "title": "D", "url": "u10", "labels": ["auto"], "status": "Ready", "blocked": True},
    {"number": 11, "title": "E", "url": "u11", "labels": ["auto"], "status": "Ready", "blocked": False},
])


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
