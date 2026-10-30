"""The claim round works per project: fairness and max_active count a
project's tasks over all its repositories, its candidates come from one
ranking across them, and a paused project claims nothing new while its
existing work carries on.

Drives dispatcher.main.run_pass with project "pe" (repositories
portfolio_eval and factorial) and project "other" (repository gamma)."""
from dataclasses import replace as dc_replace

import dispatcher.main as main
from dispatcher.github import Candidate
from dispatcher.state import PARK_WAKE, SessionRecord, Stage, load, write_session

from tests.configfakes import with_project
from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, make_task,
                             patch_usage, patch_workspace)
from tests.test_multi_project_capacity import mk_task, two_repository_cfg


def shared_cfg(tmp_path, capacity=4, **pe):
    c = two_repository_cfg(tmp_path)
    portfolio_eval, factorial = c.repositories
    shared = dc_replace(portfolio_eval.project, name="pe", **pe)
    gamma = with_project(dc_replace(
        factorial, name="gamma", repo="jesdi/gamma", github_id=3,
        clone_path=str(tmp_path / "gamma_repo"),
        worktrees_path=str(tmp_path / "gamma_repo.worktrees")), name="other")
    return dc_replace(c, capacity=capacity, repositories=[
        dc_replace(portfolio_eval, project=shared),
        dc_replace(factorial, project=shared), gamma])


def cand(number, repository):
    return Candidate(number, f"t{number}", f"u{number}", repository=repository)


def test_fewest_active_counts_every_repository_of_a_project(tmp_path, monkeypatch):
    """pe holds 2 (one per repository), other 1: the free unit goes to other,
    though every repository alone holds 1."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = shared_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "factorial", 2, slot=1)
    mk_task(c, "gamma", 3, slot=2)
    gh = FakeGitHub(cands_by_repository={
        "portfolio_eval": [cand(10, "portfolio_eval"), cand(20, "factorial")],
        "gamma": [cand(30, "gamma")]})
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == [30]


def test_repositories_of_a_project_share_its_max_active(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = shared_cfg(tmp_path, capacity=5, max_active=2)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "factorial", 2, slot=1)
    gh = FakeGitHub(cands_by_repository={
        "portfolio_eval": [cand(10, "portfolio_eval"), cand(20, "factorial")],
        "gamma": [cand(30, "gamma")]})
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2})))
    assert gh.claimed == [30], "pe is at max_active 2 across its repositories"


def test_one_ranking_claims_into_each_repository_without_number_collision(
        tmp_path, monkeypatch):
    """portfolio_eval#7 is a task already; factorial#7 is another issue and
    is claimed, into factorial, after pe's next ranked candidate."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = shared_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 7, slot=0)
    gh = FakeGitHub(cands_by_repository={"portfolio_eval": [
        cand(7, "portfolio_eval"), cand(7, "factorial"), cand(8, "portfolio_eval")]})
    sess = FakeSessions(alive={7})
    main.run_pass(c, deps(gh, sess))
    assert sess.spawn_calls == [("factorial", 7), ("portfolio_eval", 8)]
    assert load(c.state_dir, "factorial", 7).title == "t7"
    assert load(c.state_dir, "portfolio_eval", 7).title == "t", "untouched"


def test_paused_project_claims_nothing_while_its_task_still_resumes(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[with_project(c.repositories[0], paused=True)])
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    make_task(c, issue=42, stage=Stage.AWAITING_PLAN_REVIEW, park=PARK_WAKE)
    gh = FakeGitHub([Candidate(10, "Ready", "u10")])
    sess = FakeSessions()
    main.run_pass(c, deps(gh, sess))
    assert [r[0] for r in sess.resumed] == [42]
    assert gh.claimed == []
