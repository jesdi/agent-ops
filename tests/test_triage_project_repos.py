"""run_sweep resolves each repo's configuration across legacy targets,
project repositories, and the unconfigured infra repo."""
import json
import subprocess
from unittest.mock import patch

from dispatcher import triage
from dispatcher.config import Config, Project, ProjectRepository
from dispatcher.models import DEFAULT_POLICY, Entry, ModelPolicy, Track
from tests.test_board import SCHEMA, FakeGh, _issue, _node, _page
from tests.test_triage import BLOB, OLD, OK_USAGE, FakeDeps, _target


def _project_repo(repo):
    slug = repo.split("/")[-1]
    return ProjectRepository(
        repo=repo, clone_path=f"/repos/{slug}",
        worktrees_path=f"/repos/{slug}.wt", rank_cmd="", gate_cmd="make gate")


def test_sweep_reads_board_from_each_repos_own_clone(tmp_path):
    project = Project(
        key="p", name="p", board_owner="o", board_number=1,
        status_field_id="f", status_ready_option_id="r",
        status_in_progress_option_id="i", repositories=[_project_repo("o/pf")])
    cfg = Config(state_dir=str(tmp_path), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[_target("a", "o/a")],
                 projects=[project], infra_repo="o/infra")
    triage.save_cursors(tmp_path, {r: OLD for r in ("o/a", "o/pf", "o/infra")})
    empty = dict(BLOB, issues=[])
    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=empty), \
         patch.object(triage.board, "read", return_value=None) as read:
        triage.run_sweep(cfg, FakeDeps())
    # The unconfigured infra repo has no board, so it is never read.
    assert {c.args for c in read.call_args_list} == {
        ("o/a", "/repos/a"), ("o/pf", "/repos/pf")}


def test_triage_board_excludes_redacted_sibling_card(tmp_path):
    """Ticket 02, criterion 2, exercised through triage's own board lookup
    (not `board.read` directly): a shared project board's redacted card
    must never let a repo's triage sweep repair a sibling repository's
    issue, even when that sibling's issue shares the redacted card's
    title. The reviewer reproduced exactly this write through `_board`
    three rounds in a row; this pins the fix at the seam triage actually
    calls."""
    (tmp_path / ".backlog").mkdir()
    (tmp_path / ".backlog" / "project-meta.json").write_text(json.dumps(SCHEMA))
    frontend = ProjectRepository(
        repo="o/a", clone_path=str(tmp_path), worktrees_path=str(tmp_path) + ".wt",
        rank_cmd="", gate_cmd="make gate")
    project = Project(
        key="shop", name="shop", board_owner="o", board_number=1,
        status_field_id="f", status_ready_option_id="r",
        status_in_progress_option_id="i", repositories=[frontend])
    cfg = Config(state_dir=str(tmp_path), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[], projects=[project])
    redacted_backend_card = _node("REDACTED", None, "Fix X", "Ready")
    closed_frontend_issue = _issue(12, "Fix X", "CLOSED", "COMPLETED")
    gh = FakeGh([_page([redacted_backend_card])], [closed_frontend_issue])

    brd = triage._board(cfg, "o/a", [], run=gh)

    assert brd.items == {}
    assert brd.repair() == []
    assert gh.mutations == []


def _json_field(args):
    return args[args.index("--json") + 1] if "--json" in args else ""


class SharedBoardGh:
    """A shared project board (one `gh api graphql` page, both repos'
    cards) plus each repo's own `gh issue` reads — every call `run_sweep`
    makes while sweeping a multi-repository `Project`, keyed by the
    `--repo` flag every `gh issue`/`gh label` call carries."""

    def __init__(self, nodes, issue_by_repo):
        self.nodes, self.issue_by_repo = nodes, issue_by_repo
        self.mutations = []

    def __call__(self, args, capture_output=True, text=True, timeout=120,
                 env=None):
        def ok(out=""):
            return subprocess.CompletedProcess(args, 0, out, "")
        query = next((a for a in args if a.startswith("query=")), "")
        if "mutation" in query:
            self.mutations.append(" ".join(args))
            return ok()
        if args[1:3] == ["api", "graphql"]:
            return ok(_page(self.nodes))
        if args[1] == "api":  # issue-types probe: no org support here
            return subprocess.CompletedProcess(args, 1, "", "not an org")
        repo = args[args.index("--repo") + 1]
        issue = self.issue_by_repo[repo]
        fields = _json_field(args)
        if args[1:3] == ["label", "list"]:
            return ok(json.dumps([]))
        if args[1:3] == ["issue", "list"]:
            if "--search" in args:
                return ok(json.dumps([]))  # nothing updated since the cursor
            if fields == "number,title":
                return ok(json.dumps(
                    [{"number": issue["number"], "title": issue["title"]}]))
            return ok(json.dumps([issue]))  # board.read's --state all
        if args[1:3] == ["issue", "view"]:
            if fields == "comments":
                return ok(json.dumps({"comments": []}))
            return ok(json.dumps(issue))
        raise AssertionError(f"unexpected gh call: {args}")


def _collided_board_project(tmp_path):
    """One shared project board carrying `example/shop-frontend#12` and
    `example/shop-backend#12`: same issue number, same title, two distinct
    repository-qualified board items (`FE12`, `BE12`)."""
    fe_clone, be_clone = tmp_path / "fe", tmp_path / "be"
    for clone in (fe_clone, be_clone):
        (clone / ".backlog").mkdir(parents=True)
        (clone / ".backlog" / "project-meta.json").write_text(json.dumps(SCHEMA))
    frontend = ProjectRepository(
        repo="example/shop-frontend", clone_path=str(fe_clone),
        worktrees_path=str(fe_clone) + ".wt", rank_cmd="", gate_cmd="make gate")
    backend = ProjectRepository(
        repo="example/shop-backend", clone_path=str(be_clone),
        worktrees_path=str(be_clone) + ".wt", rank_cmd="", gate_cmd="make gate")
    nodes = [
        {"id": "FE12", "content": {"number": 12, "repository": {
            "nameWithOwner": "example/shop-frontend"}},
         "title": {"text": "Fix X"}, "status": None, "area": None,
         "impact": None, "effort": None},
        {"id": "BE12", "content": {"number": 12, "repository": {
            "nameWithOwner": "example/shop-backend"}},
         "title": {"text": "Fix X"}, "status": None, "area": None,
         "impact": None, "effort": None},
    ]
    issue_by_repo = {
        "example/shop-frontend": {"number": 12, "title": "Fix X", "body": "b",
                                   "labels": [], "author": {"login": "a"},
                                   "state": "OPEN", "stateReason": None},
        "example/shop-backend": {"number": 12, "title": "Fix X", "body": "b",
                                  "labels": [], "author": {"login": "a"},
                                  "state": "OPEN", "stateReason": None},
    }
    return frontend, backend, SharedBoardGh(nodes, issue_by_repo)


def test_sweep_board_write_for_one_repo_never_touches_a_collided_sibling(tmp_path):
    """Ticket 02, criterion 1: frontend #12 and backend #12 share both their
    issue number and title on the shared project board. A triage decision
    scoring frontend's #12 must write only `FE12`, never `BE12`, and
    backend's own decisions (here: none) must leave it untouched."""
    frontend, backend, gh = _collided_board_project(tmp_path)
    project = Project(
        key="shop", name="shop", board_owner="o", board_number=1,
        status_field_id="F_STATUS", status_ready_option_id="o_ready",
        status_in_progress_option_id="o_prog",
        repositories=[frontend, backend])
    cfg = Config(state_dir=str(tmp_path), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[], projects=[project])
    triage.save_cursors(tmp_path, {frontend.repo: OLD, backend.repo: OLD})

    decisions_by_repo = {
        frontend.repo: {"issues": [
            {"number": 12, "impact": 3, "effort": 1, "area": "frontend"}]},
        backend.repo: {"issues": []},
    }

    def session(cfg, repo, blob, started_date, entry, run=None):
        return decisions_by_repo[repo]

    deps = FakeDeps()
    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage, "_run_session", side_effect=session):
        triage.run_sweep(cfg, deps, run=gh)

    (mutation,) = gh.mutations
    assert "i=FE12" in mutation
    assert "i=BE12" not in mutation
    cursors = triage.load_cursors(cfg.state_dir)
    assert cursors[frontend.repo] != OLD
    assert cursors[backend.repo] != OLD


def test_paused_project_triages_every_repo_with_independent_cursors(tmp_path):
    """Ticket 02, criterion 4: a paused project's repositories (`enabled`
    is never set here, so it defaults false) still appear in `triage_repos`
    and the sweep still triages each — and each repo's write and cursor
    advance independently of its sibling's decision."""
    frontend, backend, gh = _collided_board_project(tmp_path)
    paused = Project(
        key="shop", name="shop", board_owner="o", board_number=1,
        status_field_id="F_STATUS", status_ready_option_id="o_ready",
        status_in_progress_option_id="o_prog",
        repositories=[frontend, backend])
    cfg = Config(state_dir=str(tmp_path), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[], projects=[paused])
    assert set(triage.triage_repos(cfg)) == {frontend.repo, backend.repo}
    triage.save_cursors(tmp_path, {frontend.repo: OLD, backend.repo: OLD})

    decisions_by_repo = {
        frontend.repo: {"issues": [
            {"number": 12, "impact": 3, "effort": 1, "area": "frontend"}]},
        backend.repo: {"issues": [{"number": 12, "close": {
            "kind": "not_planned", "reason": "stale"}}]},
    }

    def session(cfg, repo, blob, started_date, entry, run=None):
        return decisions_by_repo[repo]

    deps = FakeDeps()
    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage, "_run_session", side_effect=session):
        triage.run_sweep(cfg, deps, run=gh)

    # Only frontend's decision scores a board item; backend's close never
    # becomes a board mutation. Each repo's own result stays with it.
    assert len(gh.mutations) == 1 and "i=FE12" in gh.mutations[0]
    cursors = triage.load_cursors(cfg.state_dir)
    assert cursors[frontend.repo] != OLD and cursors[backend.repo] != OLD
    (_, ctx) = deps.notifier.sent[0]
    lines = ctx["lines"]
    frontend_summary = next(l for l in lines if l.startswith(frontend.repo))
    backend_summary = next(l for l in lines if l.startswith(backend.repo))
    assert "1 scored" in frontend_summary and "0 scored" in backend_summary
    assert "close #12" not in frontend_summary


def _policy_with_track(track_name):
    entry = Entry("anthropic", "opus")
    track = Track(track_name, f"only {track_name} work",
                  {s: (entry,) for s in DEFAULT_POLICY.tracks["standard"].stages})
    return ModelPolicy(triage=(entry,), untracked=track_name,
                       tracks={track_name: track})


def _triage_prompt_for(tmp_path, cfg, repo):
    """The prompt `_run_session` hands the triage container for `repo`."""
    def fake_run(cmd, **kwargs):
        triage_dir = tmp_path / triage.TRIAGE_DIR
        for f in triage_dir.glob("*-prompt.md"):
            f.with_name(f.name.replace("-prompt.md", ".json")).write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    triage._run_session(cfg, repo, {"issues": []}, "2026-01-01",
                        Entry("anthropic", "opus"), run=fake_run)
    return next((tmp_path / triage.TRIAGE_DIR).glob("*-prompt.md")).read_text()


def test_project_repository_triage_prompt_uses_its_own_model_policy(tmp_path):
    """Ticket 02, criterion 3: preserving model policy. A project repository
    with its own `models` triages with that policy's tracks; a sibling with
    none inherits the global policy."""
    own = ProjectRepository(
        repo="o/own", clone_path="/repos/own", worktrees_path="/repos/own.wt",
        rank_cmd="", gate_cmd="make gate",
        models=_policy_with_track("repo-only-track"))
    inherit = _project_repo("o/inherit")
    project = Project(
        key="p", name="p", board_owner="o", board_number=1,
        status_field_id="f", status_ready_option_id="r",
        status_in_progress_option_id="i", repositories=[own, inherit])
    cfg = Config(state_dir=str(tmp_path), capacity=2, session_memory="1500m",
                 session_cpus="2", targets=[], projects=[project],
                 models=_policy_with_track("global-only-track"))

    own_prompt = _triage_prompt_for(tmp_path, cfg, "o/own")
    for f in (tmp_path / triage.TRIAGE_DIR).iterdir():
        f.unlink()
    inherit_prompt = _triage_prompt_for(tmp_path, cfg, "o/inherit")

    assert "repo-only-track" in own_prompt
    assert "global-only-track" not in own_prompt
    assert "global-only-track" in inherit_prompt
    assert "repo-only-track" not in inherit_prompt
