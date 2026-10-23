"""Acceptance tests for ticket 06 (project-onboarding): paused projects and
exclusive unfinished-task places, driven through dispatcher.main.run_pass.

Spec: specs/project-onboarding/spec.md S19, S24, S27-S35, S53 (R10, R13,
R15-R17, R20, R21). Ticket: .agent/tickets/06-project-reservations.md.

Interface these tests define (the ticket leaves its shape open):
- run_pass claims for every `cfg.projects` entry whose `enabled` is True, and
  never for a paused one. For a project, `github.candidates(project)` and
  `github.claim(project, cand)` receive an object whose `.name` (or `.key`) is
  the project key; the claimed task is saved with `target=<project key>` and
  `repo=<the owning repository>`.
- A project's model override is the `models` of its repositories.
- Every enabled project holds one exclusive place while it has no unfinished
  task (10 places in total, whatever `cfg.max_open` says). Paused projects hold
  none, but their unfinished tasks still count. Legacy `targets` hold none.
- load_config rejects more than 10 enabled projects.
- web.read_model.next_claim takes `projects=` (the configured projects) and
  answers `reserved-places`, `total-limit` or `project-paused` where the pass
  would deny the head of that project's queue.
"""
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import state
from dispatcher.config import Project, ProjectRepository, load_config
from dispatcher.github import Candidate
from dispatcher.models import parse_policy
from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_REVIEW, PARK_WAKE,
                              SessionRecord, Stage, TaskState, load_all, save)

from tests.test_main import (POLICY, FakeGitHub, FakeSessions, cfg, deps,
                             patch_usage, patch_workspace)


class Gh(FakeGitHub):
    """Candidates by project key; remembers who each claim was for."""

    def __init__(self, cands, barrier=None):
        super().__init__()
        self.cands = dict(cands)
        self.claimed_by = []
        self.barrier = barrier

    def candidates(self, target):
        name = getattr(target, "name", None) or target.key
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return self.cands.get(name, [])

    def claim(self, target, cand):
        self.claimed_by.append((getattr(target, "name", None) or target.key, cand.number))
        super().claim(target, cand)


def project(tmp_path, key, enabled=True, models=None, repos=1):
    members = [ProjectRepository(
        repo=f"example/{key}-{i}", clone_path=str(tmp_path / f"{key}-{i}"),
        worktrees_path=str(tmp_path / f"{key}-{i}.wt"), rank_cmd="rank",
        gate_cmd="make gate", models=models) for i in range(repos)]
    return Project(key=key, name=key, board_owner="example",
                   board_number=100 + sum(map(ord, key)),
                   status_field_id="F", status_ready_option_id="R",
                   status_in_progress_option_id="I", repositories=members,
                   enabled=enabled)


def world(tmp_path, projects, capacity=20, legacy=False):
    base = cfg(tmp_path)
    return replace(base, targets=base.targets if legacy else [],
                   projects=projects, capacity=capacity)


_next_issue = iter(range(1000, 10**6))


def unfinished(c, owner, n, *, live=False, **kw):
    """n unfinished tasks of a project (or a legacy target). By default they
    are parked at a review gate: they hold a worktree, not execution capacity."""
    proj = next((p for p in c.projects if p.key == owner), None)
    tasks = []
    for _ in range(n):
        issue = next(_next_issue)
        repo = proj.repositories[0] if proj else None
        wt = Path((repo or c.targets[0]).worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        fields = (dict(stage=Stage.IMPLEMENT, slot=len(live_slots(c)))
                  if live else dict(stage=Stage.SPEC, slot=NO_SLOT, park=PARK_REVIEW))
        ts = TaskState(issue=issue, target=owner, repo=repo.repo if repo else "",
                       worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                       updated_at="2026-07-21T00:00:00+00:00", **{**fields, **kw})
        save(c.state_dir, ts)
        state.write_session(c.state_dir, owner, issue,
                            SessionRecord("sid", ts.continued_stage.value),
                            repo=ts.repo)
        tasks.append(ts)
    return tasks


def live_slots(c):
    return [t for t in load_all(c.state_dir) if t.slot != NO_SLOT]


def issues(start, n):
    return [Candidate(start + i, f"issue {start + i}", f"u{start + i}") for i in range(n)]


def run(c, gh, sess=None, passes=1):
    sess = sess or FakeSessions()
    for _ in range(passes):
        sess.alive_set = {t.issue for t in live_slots(c)}
        main.run_pass(c, deps(gh, sess))
    return sess


def open_total(c):
    return len([t for t in load_all(c.state_dir)
                if t.stage not in state.TERMINAL_STAGES])


def claimed_for(gh, key):
    return [n for k, n in gh.claimed_by if k == key]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)


# --- S19 -------------------------------------------------------------------

def _override():
    raw = {"triage": ["claude-sonnet-5@low"], "untracked": "standard",
           "tracks": {"standard": {
               "when": "Everything else.", "spec": ["claude-haiku-5"],
               "plan": ["claude-haiku-5"], "implement": ["claude-haiku-5"],
               "review": ["claude-haiku-5"]}}}
    return parse_policy(raw)


def _s19_world(tmp_path):
    return world(tmp_path, [project(tmp_path, "analytics"),
                            project(tmp_path, "billing", models=_override())])


def test_s19_inherited_and_overridden_policies_are_used(tmp_path):
    c = _s19_world(tmp_path)
    gh = Gh({"analytics": issues(100, 1), "billing": issues(200, 1)})
    sess = run(c, gh)
    models = {issue: model for issue, _stage, model, *_ in sess.spawned}
    assert models == {100: "anthropic/claude-opus-5", 200: "anthropic/claude-haiku-5"}


def test_s19_both_policies_remain_subject_to_usage_limits(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    c = _s19_world(tmp_path)
    gh = Gh({"analytics": issues(100, 1), "billing": issues(200, 1)})
    sess = run(c, gh)
    assert gh.claimed == [] and sess.spawned == []
    patch_usage(monkeypatch, util=0.2)  # allowance back: the same round claims both
    run(c, gh, sess)
    assert sorted(gh.claimed) == [100, 200]


def _fallback_override():
    stages = ["claude-fable-5-1", "claude-haiku-5"]  # first denied, second allowed
    return parse_policy({"triage": ["claude-sonnet-5@low"], "untracked": "standard",
                         "tracks": {"standard": {
                             "when": "Everything else.", "spec": stages,
                             "plan": stages, "implement": stages, "review": stages}}})


def _fallback_world(tmp_path):
    from datetime import datetime, timezone

    from dispatcher import priority
    c = world(tmp_path, [project(tmp_path, "analytics"),
                         project(tmp_path, "billing", models=_fallback_override())])
    # A fixed mode keeps the written order; auto would already rank the
    # denied fable entry last and hide the admission filter.
    priority.save(c.state_dir, "anthropic", actor="t", now=datetime.now(timezone.utc))
    return c


def test_s19_a_denied_first_model_falls_back_to_an_allowed_one(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.2, fable=0.99)  # fable denied, the rest allowed
    c = _fallback_world(tmp_path)
    gh = Gh({"analytics": issues(100, 1), "billing": issues(200, 1)})
    sess = run(c, gh)
    models = {issue: model for issue, _stage, model, *_ in sess.spawned}
    assert models == {100: "anthropic/claude-opus-5", 200: "anthropic/claude-haiku-5"}


# --- S24, S23 --------------------------------------------------------------

class _Tab:
    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return True


def test_s24_paused_project_resumes_existing_work_and_triage_but_claims_nothing(
        tmp_path, monkeypatch):
    from unittest.mock import patch
    from dispatcher import triage
    from tests.test_triage import BLOB, OK_USAGE, FakeDeps
    c = world(tmp_path, [project(tmp_path, "analytics", enabled=False)])
    (woken,) = unfinished(c, "analytics", 1, park=PARK_WAKE, track="standard")
    tab = _Tab()
    monkeypatch.setattr(triage, "running", lambda: False)
    monkeypatch.setattr(triage.herdr.Tab, "ensure", classmethod(lambda cls, *a, **k: tab))
    assert triage.enqueue(c.state_dir)  # a due scheduled request
    gh = Gh({"analytics": issues(100, 1)})
    sess = run(c, gh)
    assert [r[0] for r in sess.resumed] == [woken.issue]
    assert len(tab.commands) == 1 and "--triage-run" in tab.commands[0]
    assert gh.claimed == []
    assert [t.issue for t in load_all(c.state_dir)] == [woken.issue]
    # The launched sweep reads the paused project's repository.
    repo = c.projects[0].repositories[0].repo
    triage.save_cursors(c.state_dir, {repo: "2026-07-29T00:00:00Z"})
    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch",
                      return_value=dict(BLOB, issues=[])) as prefetch, \
         patch.object(triage.board, "read", return_value=None):
        triage.run_sweep(c, FakeDeps())
    assert [call.args[0] for call in prefetch.call_args_list] == [repo]


# --- S27 -------------------------------------------------------------------

def test_s27_tenth_unfinished_task_fits_and_the_eleventh_does_not(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics")])
    unfinished(c, "analytics", 9)
    gh = Gh({"analytics": issues(100, 3)})
    run(c, gh, passes=3)
    assert len(gh.claimed) == 1
    assert open_total(c) == 10


# --- S28 -------------------------------------------------------------------

def test_s28_simultaneous_claims_cannot_exceed_the_total(tmp_path, monkeypatch):
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "billing")])
    unfinished(c, "analytics", 8)
    unfinished(c, "billing", 1)
    barrier = threading.Barrier(2)
    admitted = threading.Barrier(2)
    provision = main.create_workspace

    def meet(*a, **k):  # both contenders get here only if both were admitted
        try:
            admitted.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return provision(*a, **k)

    monkeypatch.setattr(main, "create_workspace", meet)
    # Distinct candidates: two passes that both admit would claim twice.
    gh = [Gh({"analytics": issues(100, 1), "billing": issues(200, 1)}, barrier),
          Gh({"analytics": issues(101, 1), "billing": issues(201, 1)}, barrier)]
    errors = []

    def go(g):
        try:
            main.run_pass(c, deps(g, FakeSessions()))
        except BaseException as e:  # noqa: BLE001 — a crashed contender is a failure
            errors.append(e)

    threads = [threading.Thread(target=go, args=(g,)) for g in gh]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert len(gh[0].claimed) + len(gh[1].claimed) == 1
    assert open_total(c) == 10


# --- S29 -------------------------------------------------------------------

def test_s29_legacy_excess_drains_without_deletion(tmp_path):
    c = world(tmp_path, [project(tmp_path, "billing")], legacy=True)
    legacy = unfinished(c, "portfolio_eval", 13)
    gh = Gh({"portfolio_eval": issues(100, 3), "billing": issues(200, 3)})
    run(c, gh)
    assert gh.claimed == []
    assert len(load_all(c.state_dir)) == 13
    for done_count in (3,):  # 13 -> 10: still no place
        for t in legacy[:done_count]:
            save(c.state_dir, replace(t, stage=Stage.DONE, park=""))
    run(c, gh)
    assert gh.claimed == []
    save(c.state_dir, replace(legacy[3], stage=Stage.DONE, park=""))  # 9 left
    run(c, gh)
    assert claimed_for(gh, "billing") == [200]
    assert claimed_for(gh, "portfolio_eval") == []
    assert open_total(c) == 10


# --- S30 / S31 -------------------------------------------------------------

def three(tmp_path):
    return world(tmp_path, [project(tmp_path, k)
                            for k in ("analytics", "billing", "catalog")])


def test_s30_shared_places_stop_at_other_projects_reservations(tmp_path):
    c = three(tmp_path)
    unfinished(c, "analytics", 7)
    gh = Gh({"analytics": issues(100, 3)})
    run(c, gh, passes=3)
    assert len(gh.claimed) == 1
    assert open_total(c) == 8


def test_s31_both_empty_reservations_remain_usable(tmp_path):
    c = three(tmp_path)
    unfinished(c, "analytics", 8)
    gh = Gh({"analytics": issues(100, 2), "billing": issues(200, 2),
             "catalog": issues(300, 2)})
    run(c, gh, passes=3)
    assert sorted(k for k, _ in gh.claimed_by) == ["billing", "catalog"]
    assert open_total(c) == 10


# --- S32 -------------------------------------------------------------------

def test_s32_paused_work_counts_without_an_empty_reservation(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "billing"),
                         project(tmp_path, "catalog", enabled=False)])
    unfinished(c, "analytics", 6)
    unfinished(c, "catalog", 2)
    gh = Gh({"analytics": issues(100, 3), "billing": []})
    run(c, gh, passes=3)
    assert len(gh.claimed) == 1
    assert open_total(c) == 9


# --- pausing releases an empty reservation only ----------------------------

def test_pausing_an_empty_project_releases_its_reservation(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "catalog"),
                         project(tmp_path, "billing", enabled=False)])
    unfinished(c, "analytics", 8)
    gh = Gh({"analytics": issues(100, 3)})
    run(c, gh, passes=3)
    assert len(gh.claimed) == 1  # only catalog's place stays protected
    assert open_total(c) == 9


def test_pausing_a_project_keeps_its_work_counted(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "catalog"),
                         project(tmp_path, "billing", enabled=False)])
    unfinished(c, "analytics", 7)
    unfinished(c, "billing", 2)
    gh = Gh({"analytics": issues(100, 3)})
    run(c, gh, passes=3)
    assert gh.claimed == []
    assert open_total(c) == 9
    done = next(t for t in load_all(c.state_dir) if t.target == "billing")
    save(c.state_dir, replace(done, stage=Stage.DONE))  # billing's work drains
    run(c, gh)
    assert len(gh.claimed) == 1  # the place it frees is analytics' to take


# --- S33 -------------------------------------------------------------------

def test_s33_reservation_is_not_an_execution_slot(tmp_path):
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "billing")],
              capacity=2)
    unfinished(c, "analytics", 2, live=True)
    gh = Gh({"billing": issues(200, 1)})
    run(c, gh)
    assert gh.claimed == []
    assert len(live_slots(c)) == 2
    first = live_slots(c)[0]
    save(c.state_dir, replace(first, stage=Stage.DONE, slot=NO_SLOT))
    run(c, gh)  # a slot frees: billing's reservation lets it use it
    assert gh.claimed == [200]


def test_s33_reservation_does_not_bypass_provider_usage_limits(tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)
    c = world(tmp_path, [project(tmp_path, "analytics"), project(tmp_path, "billing")])
    gh = Gh({"billing": issues(200, 1)})
    run(c, gh)
    assert gh.claimed == []
    patch_usage(monkeypatch, util=0.2)
    run(c, gh)
    assert gh.claimed == [200]


def test_s33_reservation_with_a_denied_first_model_claims_on_the_allowed_one(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.2, fable=0.99)
    c = _fallback_world(tmp_path)
    unfinished(c, "analytics", 8)  # billing's empty reservation is the 9th place
    gh = Gh({"billing": issues(200, 1)})
    sess = run(c, gh)
    assert gh.claimed == [200]
    assert [m for _i, _s, m, *_ in sess.spawned] == ["anthropic/claude-haiku-5"]


# --- S34 / S35 (configuration) ---------------------------------------------

def test_s34_tenth_enabled_project_holds_its_place_and_no_shared_places_remain(
        tmp_path):
    keys = [f"p{i}" for i in range(10)]
    c = world(tmp_path, [project(tmp_path, k) for k in keys])
    for k in keys[:9]:
        unfinished(c, k, 1)
    gh = Gh({k: issues(100 * (i + 1), 2) for i, k in enumerate(keys)})
    run(c, gh, passes=2)
    assert [k for k, _ in gh.claimed_by] == ["p9"]
    assert open_total(c) == 10


def _yaml(tmp_path, enabled_flags, max_open=None):
    lines = [f"state_dir: {tmp_path / 'state'}", "capacity: 2",
             "session_memory: 1500m", 'session_cpus: "2"',
             "budget_threshold: 0.8", "racing_minutes: 30",
             "racing_threshold: 0.95", "pace_margin: 0.10",
             "weekend_weight: 0.5", "timezone: UTC", "targets: []", "projects:"]
    if max_open is not None:
        lines.insert(1, f"max_open: {max_open}")
    for i, on in enumerate(enabled_flags):
        lines += [f"  - key: p{i}", f"    name: p{i}", "    board_owner: example",
                  f"    board_number: {i + 1}", "    status_field_id: F",
                  "    status_ready_option_id: R", "    status_in_progress_option_id: I",
                  f"    enabled: {str(on).lower()}", "    repositories:",
                  f"      - repo: example/r{i}", f"        clone_path: /c/r{i}",
                  f"        worktrees_path: /w/r{i}", '        rank_cmd: "rank"',
                  '        gate_cmd: "make gate"']
    path = tmp_path / "targets.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_s35_an_eleventh_project_loads_paused_but_cannot_be_enabled(tmp_path):
    assert sum(p.enabled for p in load_config(_yaml(tmp_path, [True] * 10)).projects) == 10
    assert len(load_config(_yaml(tmp_path, [True] * 10 + [False])).projects) == 11
    with pytest.raises(ValueError, match="enabled"):
        load_config(_yaml(tmp_path, [True] * 11))


def test_s35_a_raised_max_open_does_not_raise_the_ten_project_limit(tmp_path):
    with pytest.raises(ValueError, match="enabled"):
        load_config(_yaml(tmp_path, [True] * 11, max_open=11))


def test_s27_a_raised_max_open_does_not_raise_the_ten_task_limit(tmp_path):
    c = replace(world(tmp_path, [project(tmp_path, "analytics")]), max_open=12)
    unfinished(c, "analytics", 10)
    gh = Gh({"analytics": issues(100, 2)})
    run(c, gh, passes=2)
    assert gh.claimed == []
    assert open_total(c) == 10


def test_s29_legacy_excess_under_a_raised_max_open_still_blocks_claims(tmp_path):
    c = replace(world(tmp_path, [project(tmp_path, "billing")], legacy=True),
                max_open=12)
    unfinished(c, "portfolio_eval", 10)
    gh = Gh({"billing": issues(200, 2)})
    run(c, gh, passes=2)
    assert gh.claimed == []
    assert open_total(c) == 10


# --- S53 -------------------------------------------------------------------

def test_s53_repository_count_does_not_increase_reservations(tmp_path):
    c = world(tmp_path, [project(tmp_path, "shop", repos=2),
                         project(tmp_path, "analytics")])
    unfinished(c, "analytics", 8)
    gh = Gh({"analytics": issues(100, 3)})
    run(c, gh, passes=3)
    # 2 exclusive places (not 3): analytics takes a 9th, never a 10th.
    assert len(gh.claimed) == 1
    assert open_total(c) == 9


# --- one task, several PRs: PR count never enters accounting ---------------

def test_task_pr_count_never_enters_unfinished_accounting(tmp_path):
    # One shop task whose work is open in every repository (PR #7 in each);
    # the task record carries the PR, the project has three repositories.
    c = world(tmp_path, [project(tmp_path, "shop", repos=3),
                         project(tmp_path, "analytics")])
    (task,) = unfinished(c, "shop", 1, stage=Stage.PR_OPEN, pr_number=7)
    unfinished(c, "analytics", 7)
    gh = Gh({"analytics": issues(100, 3)})
    gh.pr_payloads[7] = {"state": "OPEN", "mergedAt": None, "reviewDecision": "",
                         "reviews": [], "comments": []}
    run(c, gh, passes=3)
    # 8 unfinished tasks; shop's reservation is used by its one task, so
    # analytics may fill the remaining two places however many PRs shop has.
    assert len(gh.claimed) == 2
    assert open_total(c) == 10
    assert task.target == "shop"


# --- claim forecast ----------------------------------------------------------

def _forecast(tmp_path, projects, tasks, owner="analytics", capacity=20):
    from tests.test_web_read_model import GATE_OK, HB, NOW, row
    from web.read_model import next_claim
    return next_claim(HB, now=NOW, tasks=tasks, capacity=capacity, gate=GATE_OK,
                      queues=[(owner, [row(900)])], max_active={}, last_claimed={},
                      max_open=10, projects=projects)


def _three_projects(tmp_path, **paused):
    return [project(tmp_path, k, enabled=not paused.get(k, False))
            for k in ("analytics", "billing", "catalog")]


def test_forecast_explains_an_exclusive_reservation_denial(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path))
    tasks = unfinished(c, "analytics", 8)
    assert _forecast(tmp_path, c.projects, tasks).verdict == "reserved-places"


def test_forecast_agrees_with_the_pass_on_a_reservation_denial(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path))
    tasks = unfinished(c, "analytics", 8)
    gh = Gh({"analytics": issues(100, 1)})
    run(c, gh)
    assert gh.claimed == []
    assert _forecast(tmp_path, c.projects, tasks).verdict != "will-claim"


def test_forecast_explains_the_total_limit(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path))
    tasks = unfinished(c, "analytics", 10)
    assert _forecast(tmp_path, c.projects, tasks).verdict == "total-limit"


def test_forecast_explains_a_paused_project(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path, analytics=True))
    assert _forecast(tmp_path, c.projects, []).verdict == "project-paused"


def test_forecast_promises_the_last_allowed_claim(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path))
    tasks = unfinished(c, "analytics", 7)
    v = _forecast(tmp_path, c.projects, tasks)
    assert (v.verdict, v.next_target, v.next_issue) == ("will-claim", "analytics", 900)


def test_forecast_explains_an_execution_capacity_denial_without_promising_a_claim(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path), capacity=2)
    tasks = unfinished(c, "analytics", 2, live=True)
    v = _forecast(tmp_path, c.projects, tasks, capacity=2)
    assert (v.verdict, v.next_issue) == ("capacity-full", 0)


# --- the displayed forecast, through GET /api/board ---------------------------

def _board(tmp_path, projects, *, usages=None, tasks=(), rows=(900,), only=None):
    from datetime import datetime, timedelta, timezone

    from fastapi.testclient import TestClient

    from tests.test_web_routes_read import _fresh_heartbeat
    from tests.usagefakes import session_usage
    from tests.webfakes import HEADERS, FakeSources, make_config
    from web.app import create_app
    fake = FakeSources()
    fake.heartbeat = _fresh_heartbeat()
    fake.tasks_list = list(tasks)
    if usages is not None:
        fake.usages = usages
    for p in projects:
        if only is not None and p.name != only:
            continue
        fake.rank[p.name] = ([{"number": n, "title": f"t{n}", "url": "u",
                               "status": "Ready", "labels": ["auto"], "blocked": False,
                               "score": 2.0, "boost": 0} for n in rows],
                             "2026-08-01T12:00:00+00:00", False)
    conf = replace(make_config(tmp_path, capacity=20), targets=[], projects=projects)
    body = TestClient(create_app(conf, fake)).get("/api/board", headers=HEADERS).json()
    return body["next_claim"]


def test_board_forecasts_a_projects_claim(tmp_path):
    nxt = _board(tmp_path, _three_projects(tmp_path))
    assert (nxt["verdict"], nxt["next_target"], nxt["next_issue"]) == (
        "will-claim", "analytics", 900)


def test_board_forecasts_a_paused_project(tmp_path):
    nxt = _board(tmp_path, [project(tmp_path, "analytics", enabled=False)])
    assert nxt["verdict"] == "project-paused"


def test_board_forecasts_a_reservation_denial(tmp_path):
    c = world(tmp_path, _three_projects(tmp_path))
    tasks = unfinished(c, "analytics", 8)
    # only analytics has ready work: the other projects' reserved places hold no claim
    nxt = _board(tmp_path, c.projects, tasks=tasks, only="analytics")
    assert nxt["verdict"] == "reserved-places"


def test_board_forecast_does_not_promise_a_claim_the_pass_rejects_on_usage(
        tmp_path, monkeypatch):
    from tests.usagefakes import session_usage
    only_fable = parse_policy({"triage": ["claude-sonnet-5@low"],
                               "untracked": "standard", "tracks": {"standard": {
                                   "when": "Everything else.",
                                   "spec": ["claude-fable-5-1"],
                                   "plan": ["claude-fable-5-1"],
                                   "implement": ["claude-fable-5-1"],
                                   "review": ["claude-fable-5-1"]}}})
    projects = [project(tmp_path, "analytics", models=only_fable)]
    patch_usage(monkeypatch, util=0.2, fable=0.95)
    c = world(tmp_path, projects)
    gh = Gh({"analytics": issues(900, 1)})
    run(c, gh)
    assert gh.claimed == []  # the pass rejects the project's own model
    assert _board(tmp_path, projects)["verdict"] == "will-claim"  # allowance is the only difference
    nxt = _board(tmp_path, projects,
                 usages={"anthropic": session_usage(0.2, fable=0.95)})
    assert nxt["verdict"] != "will-claim"


def test_board_forecast_promises_a_claim_the_pass_makes_on_a_fallback_model(
        tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from dispatcher import priority
    from tests.usagefakes import session_usage
    patch_usage(monkeypatch, util=0.2, fable=0.99)  # first model denied, fallback allowed
    projects = [project(tmp_path, "analytics", models=_fallback_override())]
    c = world(tmp_path, projects)
    # A fixed mode keeps the written order, so the denied entry stays first.
    for state_dir in (c.state_dir, str(tmp_path)):
        priority.save(state_dir, "anthropic", actor="t", now=datetime.now(timezone.utc))
    gh = Gh({"analytics": issues(900, 1)})
    run(c, gh)
    assert gh.claimed == [900]  # the pass claims on the fallback
    nxt = _board(tmp_path, projects,
                 usages={"anthropic": session_usage(0.2, fable=0.99)})
    assert (nxt["verdict"], nxt["next_issue"]) == ("will-claim", 900)


# --- paused projects: operator input, feedback repair, CI continuation --------

def _paused_world(tmp_path, **kw):
    return world(tmp_path, [project(tmp_path, "analytics", enabled=False, **kw)])


def _reply(c, task, text):
    from dispatcher import intents
    intents.write_intent(c.state_dir, "reply", task.target, task.issue,
                         {"text": text}, actor="op@github", epoch_ms=1, repo=task.repo)


def test_s24_an_operator_reply_resumes_a_paused_projects_parked_task(tmp_path):
    c = _paused_world(tmp_path)
    (parked,) = unfinished(c, "analytics", 1, park=PARK_HUMAN, park_msg_id=55,
                           track="standard")
    _reply(c, parked, "use oauth")
    gh = Gh({"analytics": issues(100, 1)})
    sess = run(c, gh)
    assert [(r[0], "use oauth" in r[1]) for r in sess.resumed] == [(parked.issue, True)]
    assert gh.claimed == []
    assert [t.issue for t in load_all(c.state_dir)] == [parked.issue]


def test_paused_project_repairs_pr_feedback_without_claiming(tmp_path):
    c = _paused_world(tmp_path)
    (task,) = unfinished(c, "analytics", 1, stage=Stage.PR_OPEN, park="",
                         pr_number=7, feedback_pending=True, track="standard")
    gh = Gh({"analytics": issues(100, 1)})
    sess = run(c, gh)
    assert [(i, stage) for i, stage, *_ in sess.spawned] == [
        (task.issue, Stage.ADDRESS_REVIEW.value)]
    assert gh.claimed == []


def test_paused_project_continues_after_a_finished_ci_run(tmp_path):
    c = _paused_world(tmp_path)
    (task,) = unfinished(c, "analytics", 1, park=PARK_CI, ci_run_id=5,
                         track="standard")
    gh = Gh({"analytics": issues(100, 1)})
    gh.run_conclusion = "success"
    sess = run(c, gh)
    assert [(r[0], "E2E run 5 concluded: success" in r[1]) for r in sess.resumed] == [
        (task.issue, True)]
    assert gh.claimed == []


def test_s19_a_resume_intent_validates_the_model_against_the_projects_override(
        tmp_path):
    from dispatcher import intents
    c = _s19_world(tmp_path)
    (billing,) = unfinished(c, "billing", 1, park=PARK_REVIEW, track="standard")
    intents.write_intent(c.state_dir, "resume", "billing", billing.issue,
                         {"model": "anthropic/claude-haiku-5"}, actor="op@github",
                         epoch_ms=1, repo=billing.repo)
    sess = run(c, Gh({}))
    assert [(r[0], r[2]) for r in sess.resumed] == [
        (billing.issue, "anthropic/claude-haiku-5")]
