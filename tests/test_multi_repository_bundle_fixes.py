"""Ticket 11 review fixes: spec ownership gates the spec/plan stages, the plan
approval request serves the page its fingerprint covers, and legacy artifact
publication keeps the collected repository."""
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import dispatcher.main as main
from dispatcher import artifacts, intents, state, task_artifacts
from dispatcher.github import Candidate
from dispatcher.state import PARK_HUMAN, Stage, TaskBundle, TaskRepository, load, save
from tests.test_main import FakeGitHub, FakeNotifier, FakeSessions, cfg, deps, patch_usage, patch_workspace
from tests.test_multi_repository_bundle_acceptance import _origin_and_worktree
from tests.webfakes import HEADERS, make_config
from web.app import create_app
from web.sources import Sources

ORIGIN, OTHER = "jesdi/portfolio_eval", "jesdi/portfolio_api"
T, N = "portfolio_eval", 42


def _unselected_bundle(c, tmp_path):
    wt = Path(c.targets[0].worktrees_path) / f"task-{N}"
    other = tmp_path / "checkouts" / "portfolio_api"
    other.mkdir(parents=True)
    state.save_bundle(c.state_dir, T, N, TaskBundle((
        TaskRepository(ORIGIN, str(wt), f"agent/task-{N}"),
        TaskRepository(OTHER, str(other), f"agent/task-{N}"))))
    return wt, other


def _choose(c, repo):
    c = SimpleNamespace(**{**vars(c), "projects": [SimpleNamespace(
        key=T, repositories=[SimpleNamespace(repo=r) for r in (ORIGIN, OTHER)])]})
    main._apply_spec_repo_intent(c, load(c.state_dir, T, N), intents.Intent(
        "spec-repo", T, N, {"spec_repo": repo}, "op", "", Path("x"), ""))


def test_spec_stage_waits_for_the_spec_repository_choice_and_then_runs_there(tmp_path, monkeypatch):
    """A multi-repository task with no spec repository is claimed, but its spec
    stage does not start: the operator is asked to choose. A plain reply does not
    start it either. The choice starts it against the chosen checkout."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    _unselected_bundle(c, tmp_path)
    gh, sess, notifier = FakeGitHub([Candidate(N, "Two repos", "u42")]), FakeSessions(), FakeNotifier()
    d = deps(gh, sess, notifier)

    main.run_pass(c, d)
    assert gh.claimed == [N]
    assert sess.spawned == [], "the spec stage started before its repository was chosen"
    task = load(c.state_dir, T, N)
    assert (task.stage, task.park) == (Stage.SPEC, PARK_HUMAN)
    assert "parked_question" in notifier.sent
    assert Sources(c.state_dir, None, None).artifacts(T, N).spec_repo_choices == (ORIGIN, OTHER)

    main._wake(c, task, "Any news?")
    main.run_pass(c, d)
    assert sess.spawned == [] and sess.resumed == []

    _choose(c, OTHER)
    main.run_pass(c, d)
    [(issue, stage, _, prompt, _)] = sess.spawned
    assert (issue, stage) == (N, "spec")
    assert "awaiting operator selection" not in prompt
    assert f"Spec/plan repository: {OTHER}" in prompt
    assert f"/workspaces/{OTHER.replace('/', '~')}" in prompt
    assert load(c.state_dir, T, N).park == ""


def test_single_repository_bundle_starts_its_spec_stage_at_claim(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    wt = Path(c.targets[0].worktrees_path) / f"task-{N}"
    state.save_bundle(c.state_dir, T, N, TaskBundle((TaskRepository(ORIGIN, str(wt), f"agent/task-{N}"),)))
    sess = FakeSessions()
    main.run_pass(c, deps(FakeGitHub([Candidate(N, "One repo", "u42")]), sess))
    assert [s[:2] for s in sess.spawned] == [(N, "spec")]


def _spec_task(c, wt):
    task = state.TaskState(issue=N, target=T, stage=Stage.SPEC, slot=0, worktree=str(wt),
                           branch=f"agent/task-{N}", title="t", updated_at="2026-10-01T00:00:00+00:00",
                           track="standard")
    save(c.state_dir, task)
    return task


def test_plan_approval_request_serves_the_page_its_revision_covers(tmp_path, monkeypatch):
    """With the backend selected, the request content is the backend's review
    page, the one the approval fingerprint is computed over."""
    c = make_config(tmp_path)
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", c.state_dir)
    front, back = tmp_path / "front", tmp_path / "back"
    for root, text in ((front, "old frontend page"), (back, "backend page")):
        (root / ".agent").mkdir(parents=True)
        (root / ".agent" / "review.html").write_text(f'<meta name="agent-ops-review" content="1">{text}')
    (back / ".agent" / "tickets").mkdir()
    (back / ".agent" / "tickets" / "01-x.md").write_text("# 01\n")
    state.save_bundle(c.state_dir, "alpha", 7, TaskBundle((
        TaskRepository("o/front", str(front), "agent/task-7"),
        TaskRepository("o/back", str(back), "agent/task-7")), spec_repo="o/back"))
    task = state.TaskState(issue=7, target="alpha", stage=Stage.AWAITING_PLAN_REVIEW, slot=0,
                           worktree=str(front), branch="agent/task-7", title="t",
                           updated_at="2026-10-01T00:00:00+00:00")
    revision = artifacts.task_plan_revision(task, ".agent/review.html")
    save(c.state_dir, replace(task, operator_request=state.PlanApprovalRequest(
        revision.path, fingerprint=revision.fingerprint)))

    with TestClient(create_app(c, Sources(c.state_dir, None, None))) as client:
        body = client.get("/api/task/alpha/7/request", headers=HEADERS).json()
    assert body["content"]["text"].endswith("backend page")
    assert body["revision"] == revision.fingerprint


def test_legacy_publication_names_the_collected_repository(tmp_path):
    _, wt = _origin_and_worktree(tmp_path, "legacy", "agent/task-5")
    task = state.TaskState(issue=5, target="alpha", stage=Stage.PLAN, slot=0, worktree=str(wt),
                           branch="agent/task-5", title="t", updated_at="2026-10-01T00:00:00+00:00",
                           spec_path="specs/x/spec.md")
    task_artifacts.collect(tmp_path, task, "owner/repo", publish=True)
    [spec] = [i for i in task_artifacts.read(tmp_path, "alpha", 5).items if i.id == "spec"]
    assert spec.repo == ""
    assert spec.publication.url.startswith("https://github.com/owner/repo/blob/")


def test_selection_does_not_wake_a_task_that_is_not_held(tmp_path):
    c = cfg(tmp_path)
    wt, _ = _unselected_bundle(c, tmp_path)
    save(c.state_dir, replace(_spec_task(c, wt), stage=Stage.IMPLEMENT))
    _choose(c, OTHER)
    assert load(c.state_dir, T, N).park == ""
    assert state.read_bundle(c.state_dir, T, N).spec_repo == OTHER


TICKET = "# 01 x\n\n## What to build\nIt.\n\n## Blocked by\nNone.\n\n## Acceptance\n- [ ] works\n" + "." * 200


def _exploration_plan(tmp_path, monkeypatch):
    """Exploration widened the scope with no spec repository: spec and tickets
    were written into the origin checkout; the backend checkout has neither."""
    monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(tmp_path / "state"))
    c = cfg(tmp_path)
    _, front = _origin_and_worktree(tmp_path, "front", f"agent/task-{N}")
    back_origin, back = _origin_and_worktree(tmp_path, "back", f"agent/task-{N}")
    for args in (("rm", "-rq", "specs"), ("commit", "-qm", "empty"),
                 ("push", "-q", "origin", f"HEAD:refs/heads/agent/task-{N}")):
        subprocess.run(["git", "-C", str(back), *args], check=True, capture_output=True)
    (front / ".agent" / "tickets").mkdir(parents=True)
    (front / ".agent" / "tickets" / "01-x.md").write_text(TICKET)
    state.save_bundle(c.state_dir, T, N, TaskBundle((
        TaskRepository(ORIGIN, str(front), f"agent/task-{N}"),
        TaskRepository(OTHER, str(back), f"agent/task-{N}")), source="exploration"))
    task = replace(_spec_task(c, front), stage=Stage.PLAN, spec_path="specs/x/spec.md")
    save(c.state_dir, task)
    return c, task, front, back, back_origin


def test_choosing_another_repository_carries_the_explored_spec_and_tickets(tmp_path, monkeypatch):
    c, task, front, back, back_origin = _exploration_plan(tmp_path, monkeypatch)
    tickets = lambda: artifacts.check_tickets(  # noqa: E731
        Path(artifacts.task_plan_worktree(load(c.state_dir, T, N))) / artifacts.TICKETS_DIR)
    assert tickets().ok

    _choose(c, OTHER)
    assert tickets().ok, tickets().reason
    assert (back / "specs" / "x" / "spec.md").read_text() == "spec in front"
    pushed = subprocess.run(
        ["git", "--git-dir", str(back_origin), "show", f"agent/task-{N}:specs/x/spec.md"],
        capture_output=True, text=True)
    assert pushed.stdout == "spec in front"
    assert (front / "specs" / "x" / "spec.md").exists()


def test_choosing_the_origin_moves_nothing(tmp_path, monkeypatch):
    c, task, front, back, _ = _exploration_plan(tmp_path, monkeypatch)
    _choose(c, ORIGIN)
    assert not (back / "specs").exists() and not (back / ".agent").exists()
