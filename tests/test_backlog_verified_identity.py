"""Board identity must survive legacy rank scripts' title joins."""
import json

import pytest

from dispatcher import board, github
from dispatcher.config import Project, ProjectRepository
from tests.test_board import _issue, _node, _read, clone
from tests.test_github_project_backlog import _fake_run_by_clone, _project, _repo


@pytest.mark.parametrize("closed", [False, True])
def test_redacted_sibling_is_neither_scorable_nor_repairable(clone, closed):
    brd, gh = _read(clone, [_node("REDACTED_BACKEND", None, "Fix X", "Ready")],
                    [_issue(12, "Fix X", "CLOSED" if closed else "OPEN", "COMPLETED")])
    assert brd.items == {}
    assert brd.repair() == []
    assert brd.untriaged() == []
    if not closed:
        assert brd.off_board() == [12]
        with pytest.raises(ValueError, match="not awaiting a score"):
            brd.score(12, 3, 1, "infra")
    assert gh.mutations == []


@pytest.mark.parametrize("visible", [0, 1, 2])
def test_project_verifies_rank_output_against_board_identity(monkeypatch, caplog, visible):
    repos = [ProjectRepository(repo=f"example/{name}", clone_path=f"/repos/{name}",
                               worktrees_path=f"/worktrees/{name}",
                               rank_cmd="rank --json", gate_cmd="make gate")
             for name in ("frontend", "backend")]
    project = Project(key="shop", name="shop", board_owner="example", board_number=1,
                      status_field_id="F", status_ready_option_id="R",
                      status_in_progress_option_id="I", repositories=repos)
    calls = []

    def run(args, cwd=None, env=None):
        calls.append((args, cwd))
        if cwd:
            repo = next(r.repo for r in repos if r.clone_path == cwd)
            # An old rank script fills a real issue URL from the invoking
            # repository after joining a redacted board card by title.
            return json.dumps([dict(number=12, title="Fix X", status="Ready",
                                    url=f"https://github.com/{repo}/issues/12",
                                    labels=["auto"], blocked=False, score=3)])
        if args[1:3] == ["project", "view"]:
            return json.dumps({"id": "PVT"})
        assert args[1:3] == ["api", "graphql"]
        nodes = [_node("REDACTED", None, "Fix X", "Ready")]
        for repo in repos[2 - visible:]:
            node = _node(repo.repo, 12, "Fix X", "Ready")
            node["content"]["repository"]["nameWithOwner"] = repo.repo
            nodes.append(node)
        return json.dumps({"data": {"node": {"items": {
            "nodes": nodes, "pageInfo": {"hasNextPage": False}}}}})

    monkeypatch.setattr(github, "_run", run)
    candidates = github.GitHubClient().candidates(project)
    assert [(c.repo, c.number) for c in candidates] == (
        [(repo.repo, 12) for repo in repos[2 - visible:]])
    if visible < 2:
        assert "Unavailable board identity" in caplog.text
    assert len([c for c in calls if c[1]]) == 2


@pytest.mark.parametrize(
    "cards,body,state,eligible,viewed,checked",
    [
        ([("frontend", 12, "Ready"), ("backend", 12, "Ready")],
         "Blocked by: #12", "OPEN", True, False, []),
        ([("frontend", 9, "Ready")],
         "Blocked by: #9", "OPEN", True, False, []),
        ([("frontend", 9, "Ready"), ("backend", 9, "Done")],
         None, "OPEN", True, True, []),
        ([("frontend", 9, "Ready"), ("backend", 9, "Done")],
         "Mention #9 outside the dependency field", "OPEN", True, True, []),
        ([("frontend", 9, "Ready"), ("backend", 9, "Done")],
         "Details\n  bLoCkEd By: #9, #99\nBlocked by: #9", "OPEN",
         False, True, [9]),
        ([("frontend", 9, "Ready"), ("backend", 9, "Done")],
         "Blocked by: #9", "CLOSED", True, True, [9]),
        ([("frontend", 9, "Done"), ("backend", 9, "Ready")],
         "Blocked by: #9", "OPEN", True, True, []),
        ([("backend", 9, "Ready"), ("other", 9, "Ready")],
         "Blocked by: #9", "OPEN", True, True, []),
    ],
    ids=["self-collision", "unique-number", "empty-body", "ordinary-mention",
         "open-local-blocker", "closed-local-blocker", "done-local-blocker",
         "missing-local-blocker"],
)
def test_candidates_recheck_colliding_blockers_in_the_owning_repo(
        monkeypatch, cards, body, state, eligible, viewed, checked):
    frontend, backend, other = [_repo(f"example/{name}")
                                for name in ("frontend", "backend", "other")]
    project = _project("shop", [frontend, backend, other])
    row = dict(number=12, title="Work", status="Ready", labels=["auto"],
               blocked=False, url="https://github.com/example/frontend/issues/12")
    mapping = {frontend.clone_path: [row], backend.clone_path: [], other.clone_path: []}
    board_items = [(frontend.repo, 12, "Ready")]
    board_items.extend((f"example/{repo}", number, status)
                       for repo, number, status in cards
                       if (repo, number) != ("frontend", 12))
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))
    client = github.GitHubClient()
    views, states = [], []

    def issue_view(repo, number):
        views.append((repo, number))
        return {"body": body}

    def issue_state(repo, number):
        states.append((repo, number))
        return state

    monkeypatch.setattr(client, "issue_view", issue_view)
    monkeypatch.setattr(client, "issue_state", issue_state)

    candidates = client.candidates(project)

    assert [(c.repo, c.number) for c in candidates] == (
        [(frontend.repo, 12)] if eligible else [])
    assert views == ([(frontend.repo, 12)] if viewed else [])
    assert states == [(frontend.repo, number) for number in checked]


@pytest.mark.parametrize("rank_blocked", [False, True])
@pytest.mark.parametrize("local_status", ["Ready", "Done"])
def test_rechecking_collisions_preserves_other_local_dependencies(
        monkeypatch, rank_blocked, local_status):
    frontend, backend = [_repo(f"example/{name}")
                         for name in ("frontend", "backend")]
    project = _project("shop", [frontend, backend])
    row = dict(number=12, title="Work", status="Ready", labels=["auto"],
               blocked=rank_blocked, url="u12")
    mapping = {frontend.clone_path: [row], backend.clone_path: []}
    cards = [(frontend.repo, 12, "Ready"), (frontend.repo, 9, "Done"),
             (backend.repo, 9, "Ready"), (frontend.repo, 10, local_status)]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, cards))
    client = github.GitHubClient()
    monkeypatch.setattr(client, "issue_view",
                        lambda repo, number: {"body": "Blocked by: #9, #10"})
    states = []

    def issue_state(repo, number):
        states.append((repo, number))
        return "OPEN"

    monkeypatch.setattr(client, "issue_state", issue_state)

    candidates = client.candidates(project)

    assert [(c.repo, c.number) for c in candidates] == (
        [(frontend.repo, 12)] if local_status == "Done" else [])
    assert states == ([(frontend.repo, 10)] if local_status == "Ready" else [])
