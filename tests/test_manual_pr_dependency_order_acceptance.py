"""Acceptance tests for ticket 13 (project-onboarding): the task view shows the
prerequisite order for manually merging a task's required PRs.

Spec: specs/project-onboarding/spec.md requirement 25, scenarios S62, S63, S64.
Ticket: .agent/tickets/13-manual-pr-dependency-order.md. Seam: TASK_API, the
qualified task detail route driven through web.app.create_app over the real
web.sources.Sources and a tmp state dir.

Interface these tests define (the ticket leaves its shape open):
- `state.RequiredPR(..., depends_on=("owner/name#N", ...))` declares the
  prerequisite edges of one required PR, each a repository-qualified PR of the
  same task's required set.
- `GET /api/task/{project}/{repository_id}/{issue}` returns, per entry of
  `required_prs`, `depends_on` (the declared edges, echoed), `dependency` and
  `waiting_on`. `dependency` is judged apart from `checks` and `outcome`:
    "satisfied"  every prerequisite is merged (or there is none);
    "waiting"    an open/closed prerequisite is not merged; `waiting_on` lists
                 those "owner/name#N" refs;
    "invalid"    the PR has a self, cyclic or missing-required-PR edge.
  The task detail also carries `dependency_errors`: a non-empty list of
  messages naming the offending "owner/name#N" refs when any edge is invalid,
  and [] otherwise.
- Nothing merges a PR: viewing the task never changes a PR's state or finishes
  the task.
"""
import pytest
from fastapi.testclient import TestClient

import dispatcher.main as main

from dispatcher import intents, state
from dispatcher.state import Stage
from tests.test_main import FakeNotifier, deps, patch_usage, patch_workspace
from tests.test_required_pr_lifecycle_acceptance import RepoGh, pr_payload
from tests.test_project_reservations_acceptance import project, unfinished, world
from tests.webfakes import HEADERS, make_config, make_target, make_task
from web.app import create_app
from web.sources import Sources

PROJECT = "shop"
FRONT, BACK = "example/shop-frontend", "example/shop-backend"
ISSUE = 12
F9, B7 = f"{FRONT}#9", f"{BACK}#7"


class FakeGitHub:
    def rank_rows(self, target):
        return []

    def issue_state(self, repo, number):
        return "OPEN"


class FakeSessions:
    def capture_tail(self, target, issue, lines=25, repo=""):
        return ""

    def capture_history(self, target, issue, lines=2000, repo=""):
        return ""

    def is_alive(self, target, issue, repo=""):
        return False


def pr(repo, number, **kw):
    kw.setdefault("checks", "passing")
    return state.RequiredPR(repo, number, **kw)


def detail(tmp_path, required, task_repo=FRONT):
    """The task detail JSON of one unfinished shop task with these required PRs."""
    state.save(tmp_path, make_task(
        issue=ISSUE, target=PROJECT, repo=task_repo, stage=Stage.PR_OPEN,
        required_prs=tuple(required), required_set_revision=1))
    cfg = make_config(tmp_path, targets=[make_target(PROJECT, FRONT)])
    client = TestClient(create_app(
        cfg, Sources(str(tmp_path), FakeSessions(), FakeGitHub(), systemctl=("true",))))
    r = client.get(f"/api/task/{PROJECT}/{task_repo.replace('/', '~')}/{ISSUE}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def views(body):
    return {f"{p['repo']}#{p['number']}": p for p in body["required_prs"]}


def stored(tmp_path):
    return {f"{p.repo}#{p.number}": p.state
            for p in state.load(tmp_path, PROJECT, ISSUE, repo=FRONT).required_prs}


def test_s62_passing_checks_do_not_satisfy_an_unmerged_prerequisite(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7), pr(FRONT, 9, depends_on=(B7,))])

    v = views(body)
    assert v[F9]["checks"] == v[B7]["checks"] == "passing"
    assert v[F9]["dependency"] == "waiting"
    assert v[F9]["waiting_on"] == [B7]
    assert v[F9]["depends_on"] == [B7]
    assert v[B7]["dependency"] == "satisfied" and v[B7]["waiting_on"] == []
    assert body["dependency_errors"] == []
    # the pipeline merged nothing
    assert stored(tmp_path) == {B7: "open", F9: "open"}


def test_a_prerequisite_awaiting_human_review_is_still_unmet(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7, outcome="feedback", checks="passing"),
                             pr(FRONT, 9, depends_on=(B7,))])

    assert views(body)[F9]["dependency"] == "waiting"
    assert views(body)[F9]["waiting_on"] == [B7]


def test_s63_a_merged_prerequisite_makes_its_dependent_eligible_in_order(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7, state="merged"), pr(FRONT, 9, depends_on=(B7,))])

    v = views(body)
    assert v[F9]["dependency"] == "satisfied" and v[F9]["waiting_on"] == []
    assert v[F9]["state"] == "open" and v[F9]["checks"] == "passing"
    # not merged for the operator, and the task is not complete while frontend #9 is open
    assert stored(tmp_path) == {B7: "merged", F9: "open"}
    assert body["card"]["stage"] == "pr-open"


def test_s64_independent_prs_are_not_ordered_by_task_membership(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7), pr(FRONT, 9)])

    v = views(body)
    for ref in (B7, F9):
        assert v[ref]["dependency"] == "satisfied"
        assert v[ref]["waiting_on"] == [] and v[ref]["depends_on"] == []
    assert body["dependency_errors"] == []
    assert stored(tmp_path) == {B7: "open", F9: "open"}


def test_dependency_follows_repository_qualified_identity(tmp_path):
    """Frontend #7 depends on backend #7: a merged frontend #7 does not satisfy it."""
    body = detail(tmp_path, [pr(BACK, 7), pr(FRONT, 7, state="merged"),
                             pr(FRONT, 9, depends_on=(f"{BACK}#7",))])

    v = views(body)
    assert v[F9]["dependency"] == "waiting"
    assert v[F9]["waiting_on"] == [B7]


def test_a_self_edge_is_a_visible_validation_error_not_a_ready_state(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7), pr(FRONT, 9, depends_on=(F9,))])

    assert views(body)[F9]["dependency"] == "invalid"
    assert any(F9 in e for e in body["dependency_errors"])


def test_a_cycle_is_a_visible_validation_error_not_a_ready_state(tmp_path):
    body = detail(tmp_path, [pr(BACK, 7, depends_on=(F9,)), pr(FRONT, 9, depends_on=(B7,))])

    v = views(body)
    assert v[B7]["dependency"] == v[F9]["dependency"] == "invalid"
    assert any(B7 in e and F9 in e for e in body["dependency_errors"])


def test_an_edge_to_a_pr_outside_the_required_set_is_a_visible_validation_error(tmp_path):
    missing = f"{BACK}#99"
    body = detail(tmp_path, [pr(BACK, 7), pr(FRONT, 9, depends_on=(missing,))])

    assert views(body)[F9]["dependency"] == "invalid"
    assert any(missing in e for e in body["dependency_errors"])


@pytest.fixture
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "remove_workspace", lambda *a, **k: None)


def test_s63_polling_one_merged_prerequisite_leaves_the_task_unfinished_and_the_dependent_eligible(
        tmp_path, _env):
    """The dispatcher polls a merged backend #7; frontend #9 is then eligible for the
    operator, nothing merges it, and the task does not complete."""
    c = world(tmp_path, [project(tmp_path, "shop", repos=2)])
    front, back = "example/shop-0", "example/shop-1"
    (t,) = unfinished(c, "shop", 1, stage=Stage.PR_OPEN, park="", required_set_revision=1,
                      required_prs=(pr(back, 7), pr(front, 9, depends_on=(f"{back}#7",))))
    gh = RepoGh()
    gh.prs[(back, 7)] = pr_payload("MERGED")
    gh.prs[(front, 9)] = pr_payload("OPEN", head="f0f0f0f0")
    notif = FakeNotifier()

    main.run_pass(c, deps(gh, notifier=notif))

    client = TestClient(create_app(c, Sources(
        str(c.state_dir), FakeSessions(), FakeGitHub(), systemctl=("true",))))
    body = client.get(f"/api/task/shop/{front.replace('/', '~')}/{t.issue}", headers=HEADERS).json()
    v = views(body)
    assert v[f"{back}#7"]["state"] == "merged"
    assert v[f"{front}#9"]["state"] == "open"
    assert v[f"{front}#9"]["dependency"] == "satisfied" and v[f"{front}#9"]["waiting_on"] == []
    assert body["card"]["stage"] == "pr-open"
    assert "task_done" not in notif.sent


def _queue_and_pass(c, t, front, action, payload, gh):
    intents.write_intent(c.state_dir, action, "shop", t.issue,
                         {"revision": 1, "pr_repo": front, **payload}, "operator", 1, repo=front)
    main.run_pass(c, deps(gh, notifier=FakeNotifier()))
    client = TestClient(create_app(c, Sources(
        str(c.state_dir), FakeSessions(), FakeGitHub(), systemctl=("true",))))
    r = client.get(f"/api/task/shop/{front.replace('/', '~')}/{t.issue}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return views(r.json())


def test_a_queued_depend_pr_intent_is_applied_and_the_dependent_waits(tmp_path, _env):
    c = world(tmp_path, [project(tmp_path, "shop", repos=2)])
    front, back = "example/shop-0", "example/shop-1"
    (t,) = unfinished(c, "shop", 1, stage=Stage.PR_OPEN, park="", required_set_revision=1,
                      required_prs=(pr(back, 7), pr(front, 9)))
    gh = RepoGh()
    gh.prs[(back, 7)] = pr_payload("OPEN", head="b0b0b0b0")
    gh.prs[(front, 9)] = pr_payload("OPEN", head="f0f0f0f0")

    v = _queue_and_pass(c, t, front, "depend-pr",
                        {"number": 9, "depends_on": [f"{back}#7"]}, gh)

    assert v[f"{front}#9"]["depends_on"] == [f"{back}#7"]
    assert v[f"{front}#9"]["dependency"] == "waiting"
    assert v[f"{front}#9"]["waiting_on"] == [f"{back}#7"]
    assert v[f"{back}#7"]["state"] == "open"


def test_replacing_a_closed_pr_keeps_its_prerequisites_and_the_replacement_waits(tmp_path, _env):
    c = world(tmp_path, [project(tmp_path, "shop", repos=2)])
    front, back = "example/shop-0", "example/shop-1"
    (t,) = unfinished(c, "shop", 1, stage=Stage.PR_OPEN, park="", required_set_revision=1,
                      required_prs=(pr(back, 7), pr(front, 9, state="closed",
                                                    depends_on=(f"{back}#7",))))
    gh = RepoGh()
    gh.prs[(back, 7)] = pr_payload("OPEN", head="b0b0b0b0")
    gh.prs[(front, 9)] = pr_payload("CLOSED", head="f0f0f0f0")
    gh.prs[(front, 10)] = pr_payload("OPEN", head="f1f1f1f1")

    v = _queue_and_pass(c, t, front, "replace-pr", {"number": 9, "replacement": 10}, gh)

    assert f"{front}#9" not in v
    assert v[f"{front}#10"]["depends_on"] == [f"{back}#7"]
    assert v[f"{front}#10"]["dependency"] == "waiting"
    assert v[f"{front}#10"]["waiting_on"] == [f"{back}#7"]
