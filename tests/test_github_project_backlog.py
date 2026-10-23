"""Acceptance tests for ticket 02 (BACKLOG seam): `GitHubClient.rank_rows`
and `.candidates` carry repository identity across a project's member
repositories, instead of a single `Target`'s repo. `Project` and
`ProjectRepository` are built directly (ticket 01's CONFIG seam); the seam
under test here is `dispatcher/github.py` only."""
import json

import dispatcher.github as github
from dispatcher.config import Project, ProjectRepository, Target


def _repo(repo: str, rank_cmd: str = "rank") -> ProjectRepository:
    slug = repo.split("/")[-1]
    return ProjectRepository(
        repo=repo, clone_path=f"/repos/{slug}",
        worktrees_path=f"/repos/{slug}.worktrees",
        rank_cmd=rank_cmd, gate_cmd="make gate")


def _project(key: str, repositories, enabled: bool = False) -> Project:
    return Project(
        key=key, name=key, board_owner="example", board_number=1,
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="I", repositories=repositories,
        enabled=enabled)


def _board_item_node(item_id: str, repo: str | None, number: int | None,
                     status: str | None = None, title: str = "") -> dict:
    """One GraphQL `items.nodes` entry. `repo`/`number` None models a
    redacted card: the project-scope token could not expand its linked
    issue, so GitHub reports no `content.repository`/`content.number`."""
    content = ({"number": number, "repository": {"nameWithOwner": repo}}
               if repo is not None and number is not None else {})
    return {"id": item_id, "content": content, "title": {"text": title},
            "status": {"name": status} if status else None}


def _fake_run_by_clone(mapping: dict[str, list[dict]],
                        board_items: list[tuple]):
    """`_run` stand-in for two call shapes a repository-qualified `Project`
    makes: one rank_cmd invocation per repo clone, keyed by `cwd`, and the
    shared-board lookup (`gh project view` then one `gh api graphql` page)
    that returns only the repository-qualified `(repo, number)` pairs in
    `board_items` as verified board identity. A rank_cmd row whose
    `(repo, number)` is absent from `board_items` is not on the real board
    under that identity and must come back unavailable — a cwd absent from
    `mapping` is a bug in the test setup, not a production behavior to
    tolerate."""
    # A board item is `(repo, number, status)` or
    # `(repo, number, status, title)`; a missing status is an unset card; `repo`/`number` None is a redacted card.
    nodes = [_board_item_node(f"PVTI_{i}", *item)
             for i, item in enumerate(board_items)]
    items_page = json.dumps({"data": {"node": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes}}}})

    def fake_run(args, cwd=None, **kwargs):
        if args[:3] == ["gh", "project", "view"]:
            return json.dumps({"id": "PVT_shop"})
        if args[:3] == ["gh", "api", "graphql"]:
            return items_page
        return json.dumps(mapping[cwd])
    return fake_run


LEGACY_TARGET = Target(
    name="solo", repo="example/solo",
    clone_path="/repos/solo", worktrees_path="/repos/solo.worktrees",
    rank_cmd="rank", project_number=1, project_owner="example",
    status_field_id="F", status_ready_option_id="R",
    status_in_progress_option_id="I")


def test_identical_issue_numbers_across_repos_remain_distinct_candidates(monkeypatch):
    """Ticket 02, criterion 1: frontend #12 and backend #12 are two distinct
    candidates, each carrying its own repository identity."""
    frontend = _repo("example/shop-frontend")
    backend = _repo("example/shop-backend")
    project = _project("shop", [frontend, backend])
    mapping = {
        frontend.clone_path: [{"number": 12, "title": "Frontend bug",
                               "url": "uf12", "labels": ["auto"],
                               "status": "Ready", "blocked": False}],
        backend.clone_path: [{"number": 12, "title": "Backend bug",
                              "url": "ub12", "labels": ["auto"],
                              "status": "Ready", "blocked": False}],
    }
    board_items = [(frontend.repo, 12, "Ready"), (backend.repo, 12, "Ready")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    got = github.GitHubClient().candidates(project)

    assert len(got) == 2
    by_repo = {c.repo: c for c in got}
    assert by_repo[frontend.repo].number == 12
    assert by_repo[backend.repo].number == 12
    assert by_repo[frontend.repo].title == "Frontend bug"
    assert by_repo[backend.repo].title == "Backend bug"


def test_unverified_row_is_unavailable_not_matched_by_title_alone(monkeypatch):
    """Ticket 02, criterion 2: a row with no verified repository-qualified
    issue number (redacted board content) never becomes a candidate, and
    sharing a title with a different repo's resolved issue must not stand
    in for that missing identity.

    The frontend rank_cmd here is a legacy script that title-joined a
    redacted shared-board card to "Fix X" and filled in its own repo's
    issue URL for #12 — the URL alone looks like a genuine frontend issue,
    so the per-row URL self-check cannot catch it. Only the real board
    (`board_items`, confirming (backend, 12) but never (frontend, 12))
    exposes that frontend's #12 is unverified."""
    frontend = _repo("example/shop-frontend")
    backend = _repo("example/shop-backend")
    project = _project("shop", [frontend, backend])
    mapping = {
        frontend.clone_path: [{"number": 12, "title": "Fix X",
                               "url": "https://github.com/example/shop-frontend/issues/12",
                               "labels": ["auto"], "status": "Ready",
                               "blocked": False}],
        backend.clone_path: [{"number": 12, "title": "Fix X",
                              "url": "https://github.com/example/shop-backend/issues/12",
                              "labels": ["auto"], "status": "Ready",
                              "blocked": False}],
    }
    board_items = [(backend.repo, 12, "Ready")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    got = github.GitHubClient().candidates(project)

    assert len(got) == 1
    assert got[0].repo == backend.repo
    assert got[0].number == 12


def test_candidate_status_comes_from_its_own_board_card_not_a_title_sibling(monkeypatch):
    """Ticket 02, criteria 2 and 3: frontend #12 is Inbox on the board. A
    legacy rank_cmd title-joined it to a redacted card (a backend card,
    also titled "Fix X") that is Ready, so the row reports `Ready`. The
    Ready status belongs to a different card; frontend #12 must not become
    a candidate."""
    frontend = _repo("example/shop-frontend")
    project = _project("shop", [frontend])
    mapping = {frontend.clone_path: [{
        "number": 12, "title": "Fix X",
        "url": "https://github.com/example/shop-frontend/issues/12",
        "labels": ["auto"], "status": "Ready", "blocked": False}]}
    board_items = [(frontend.repo, 12, "Inbox", "Fix X"),
                   (None, None, "Ready", "Fix X")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    assert github.GitHubClient().candidates(project) == []


def test_unset_board_status_never_makes_a_candidate(monkeypatch):
    """Ticket 02, criteria 2 and 3: the verified card is the only source of
    status. A rank row that says `Ready` for a card whose board status is
    unset (cleared after ranking, or joined from a sibling) is not eligible."""
    frontend = _repo("example/shop-frontend")
    project = _project("shop", [frontend])
    mapping = {frontend.clone_path: [{
        "number": 12, "title": "Fix X", "url": "u12", "labels": ["auto"],
        "status": "Ready", "blocked": False}]}
    board_items = [(frontend.repo, 12, None)]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    assert github.GitHubClient().candidates(project) == []


def _blocker_collision(monkeypatch, *, blocked, frontend_9, backend_9):
    """frontend #12 says `Blocked by: #9`; #9 exists in both repos with the
    given board statuses. `blocked` is what a legacy rank script (bare-number
    blocker join) reported for the row."""
    frontend = _repo("example/shop-frontend")
    backend = _repo("example/shop-backend")
    project = _project("shop", [frontend, backend])
    mapping = {
        frontend.clone_path: [{
            "number": 12, "title": "Work", "url": "u12", "labels": ["auto"],
            "status": "Ready", "blocked": blocked}],
        backend.clone_path: []}
    board_items = [(frontend.repo, 12, "Ready"), (frontend.repo, 9, frontend_9),
                   (backend.repo, 9, backend_9)]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))
    client = github.GitHubClient()
    monkeypatch.setattr(client, "issue_view",
                        lambda repo, number: {"body": "Blocked by: #9"})
    monkeypatch.setattr(client, "issue_state", lambda repo, number: "OPEN")
    return [(c.repo, c.number) for c in client.candidates(project)]


def test_open_blocker_in_own_repo_blocks_despite_done_sibling_number(monkeypatch):
    """Ticket 02, criterion 3: a legacy rank script joined `#9` by bare
    number, found the Done backend #9, and reported frontend #12 unblocked.
    Frontend's own #9 is still open, so #12 must stay blocked."""
    got = _blocker_collision(monkeypatch, blocked=False,
                             frontend_9="Ready", backend_9="Done")

    assert got == []


def test_done_blocker_in_own_repo_unblocks_despite_open_sibling_number(monkeypatch):
    """Ticket 02, criterion 3: a legacy rank script joined `#9` to the open
    backend #9 and reported frontend #12 blocked. Frontend's own #9 is Done,
    so #12 is eligible."""
    got = _blocker_collision(monkeypatch, blocked=True,
                             frontend_9="Done", backend_9="Ready")

    assert got == [("example/shop-frontend", 12)]


def test_project_wide_ranking_merges_repos_by_score_not_concatenation(monkeypatch):
    """Ticket 02, criterion 3: ranking a project merges every member repo
    into one ranked order (project-wide fairness target), rather than
    concatenating each repo's own rank_cmd output after the other."""
    frontend = _repo("example/shop-frontend")
    backend = _repo("example/shop-backend")
    project = _project("shop", [frontend, backend])
    mapping = {
        frontend.clone_path: [
            {"number": 1, "title": "F-high", "url": "u1", "labels": ["auto"],
             "status": "Ready", "blocked": False, "score": 3.0},
            {"number": 2, "title": "F-low", "url": "u2", "labels": ["auto"],
             "status": "Ready", "blocked": False, "score": 1.0},
        ],
        backend.clone_path: [
            {"number": 3, "title": "B-mid", "url": "u3", "labels": ["auto"],
             "status": "Ready", "blocked": False, "score": 2.0},
        ],
    }
    board_items = [(frontend.repo, 1, "Ready"), (frontend.repo, 2, "Ready"),
                   (backend.repo, 3, "Ready")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    rows = github.GitHubClient().rank_rows(project)

    # Concatenation would give [1, 2, 3]; a real score merge gives [1, 3, 2].
    assert [r["number"] for r in rows] == [1, 3, 2]


def test_project_wide_candidates_keep_eligibility_gates_and_boost_priority(monkeypatch):
    """Ticket 02, criterion 3: adding a repository must not weaken the
    existing eligibility gates (Ready status, not blocked, `auto` label)
    or drop an operator's boost priority across repositories. Every
    non-eligible row below shares the highest score, so only the gate
    itself — not score — can be excluding it; the eligible backend row
    carries a boost that must still outrank the eligible frontend row's
    higher score."""
    frontend = _repo("example/shop-frontend")
    backend = _repo("example/shop-backend")
    project = _project("shop", [frontend, backend])
    mapping = {
        frontend.clone_path: [
            {"number": 1, "title": "F-not-ready", "url": "u1",
             "labels": ["auto"], "status": "InProgress", "blocked": False,
             "score": 9.0},
            {"number": 2, "title": "F-blocked", "url": "u2",
             "labels": ["auto"], "status": "Ready", "blocked": True,
             "score": 9.0},
            {"number": 3, "title": "F-no-auto-label", "url": "u3",
             "labels": [], "status": "Ready", "blocked": False,
             "score": 9.0},
            {"number": 4, "title": "F-eligible", "url": "u4",
             "labels": ["auto"], "status": "Ready", "blocked": False,
             "score": 3.0, "boost": 0},
        ],
        backend.clone_path: [
            {"number": 5, "title": "B-eligible-boosted", "url": "u5",
             "labels": ["auto"], "status": "Ready", "blocked": False,
             "score": 1.0, "boost": 99},
        ],
    }
    board_items = [(frontend.repo, 1, "InProgress"), (frontend.repo, 2, "Ready"),
                   (frontend.repo, 3, "Ready"), (frontend.repo, 4, "Ready"),
                   (backend.repo, 5, "Ready")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    got = github.GitHubClient().candidates(project)

    # Rows 1-3 each fail exactly one eligibility gate despite the highest
    # score, so only the two eligible rows (4, 5) may appear — ordered by
    # backend's operator boost ahead of frontend's higher raw score.
    assert [c.number for c in got] == [5, 4]


def test_paused_project_membership_still_ranks_and_yields_candidates(monkeypatch):
    """Ticket 02, criterion 4: a paused project's membership remains
    available to ranking/triage — rank_rows and candidates do not gate on
    `Project.enabled`."""
    repo = _repo("example/analytics")
    paused = _project("analytics", [repo], enabled=False)
    mapping = {repo.clone_path: [{"number": 5, "title": "T", "url": "u5",
                                  "labels": ["auto"], "status": "Ready",
                                  "blocked": False}]}
    board_items = [(repo.repo, 5, "Ready")]
    monkeypatch.setattr(github, "_run", _fake_run_by_clone(mapping, board_items))

    got = github.GitHubClient().candidates(paused)

    assert [c.number for c in got] == [5]


def test_legacy_single_repository_target_still_ranks_without_a_project_join(monkeypatch):
    """Ticket 02, criterion 5: a legacy single-repository `Target` keeps
    ranking with exactly one rank_cmd call (no multi-repository join), and
    its candidates now also carry that repo's identity."""
    calls = []

    def fake_run(args, cwd=None, **kwargs):
        calls.append(cwd)
        return json.dumps([{"number": 9, "title": "Solo", "url": "u9",
                            "labels": ["auto"], "status": "Ready",
                            "blocked": False}])

    monkeypatch.setattr(github, "_run", fake_run)

    got = github.GitHubClient().candidates(LEGACY_TARGET)

    assert calls == [LEGACY_TARGET.clone_path]
    assert got[0].repo == LEGACY_TARGET.repo
    assert got[0].number == 9
