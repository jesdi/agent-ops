"""Acceptance tests for ticket 02: one box-wide capacity across targets.

Black-box: drives dispatcher.main.run_pass(cfg, deps) with two targets
(portfolio_eval, factorial) and asserts on the resulting task state, claims,
and event log — never on main.py internals.
"""
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher import eventlog
from dispatcher.github import Candidate
from dispatcher.state import (IN_FLIGHT_STAGES, PARK_LOGIN, PARK_WAKE, Stage,
                              TaskState, active, load, load_all, save)

from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps,
                             patch_usage, patch_workspace)


def two_target_cfg(tmp_path):
    """portfolio_eval (the default target from cfg()) plus factorial, a
    second target with its own repo/worktree root."""
    c = cfg(tmp_path)
    factorial = dc_replace(
        c.targets[0], name="factorial", repo="jesdi/factorial",
        clone_path=str(tmp_path / "factorial_repo"),
        worktrees_path=str(tmp_path / "factorial_repo.worktrees"),
        project_number=2)
    return dc_replace(c, targets=[c.targets[0], factorial])


def mk_task(c, target_name, issue, stage=Stage.IMPLEMENT, slot=0,
           updated_at="2026-07-21T00:00:00+00:00", track="standard", **kw):
    tgt = next(t for t in c.targets if t.name == target_name)
    wt = Path(tgt.worktrees_path) / f"task-{issue}"
    (wt / ".agent").mkdir(parents=True, exist_ok=True)
    ts = TaskState(issue=issue, target=target_name, stage=stage, slot=slot,
                   worktree=str(wt), branch=f"agent/task-{issue}", title="t",
                   updated_at=updated_at, track=track, **kw)
    save(c.state_dir, ts)
    return wt


def wake_blocked_events(c, target, issue):
    return [e for e in eventlog.read_tail(c.state_dir)
            if e["event"] == "wake-blocked" and e["target"] == target
            and e["issue"] == issue]


# --- checkbox: capacity is shared across targets -> a pass claims nothing --

def test_capacity_shared_across_targets_blocks_all_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 3, slot=2)
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main_deps = deps(gh, FakeSessions(alive={1, 2, 3}))
    main.run_pass(c, main_deps)
    assert gh.claimed == []


# --- checkbox: boundary, last unit is used -------------------------------

def test_capacity_boundary_claims_exactly_one_and_fills_box(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    gh = FakeGitHub(cands_by_target={
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2})))
    assert gh.claimed == [20]
    assert load(c.state_dir, "factorial", 20) is not None
    box_active = [t for t in load_all(c.state_dir) if t.stage in IN_FLIGHT_STAGES]
    assert len(active(box_active)) == 3


# --- checkbox: a login-parked task still counts box-wide ------------------

def test_login_parked_task_counts_box_wide(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 3, slot=2, park=PARK_LOGIN)
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == []


# --- checkbox: a woken task is refused when the box is full ---------------

def test_woken_task_refused_when_box_is_full(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "portfolio_eval", 3, slot=2)
    mk_task(c, "factorial", 42, stage=Stage.IMPLEMENT, park=PARK_WAKE, slot=3)
    sess = FakeSessions(alive={1, 2, 3})
    main.run_pass(c, deps(sess=sess))
    saved = load(c.state_dir, "factorial", 42)
    assert saved.park == PARK_WAKE
    assert (42 not in [r[0] for r in sess.resumed])
    assert wake_blocked_events(c, "factorial", 42) != []
    assert wake_blocked_events(c, "factorial", 42)[-1]["detail"] == "capacity full"


def test_feedback_pending_pr_refused_when_box_is_full(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "portfolio_eval", 3, slot=2)
    mk_task(c, "factorial", 42, stage=Stage.PR_OPEN, feedback_pending=True,
           pr_number=7, slot=3)
    sess = FakeSessions(alive={1, 2, 3})
    main.run_pass(c, deps(sess=sess))
    saved = load(c.state_dir, "factorial", 42)
    assert saved.feedback_pending is True
    assert (42 not in [s[0] for s in sess.spawned])
    assert wake_blocked_events(c, "factorial", 42) != []
    assert wake_blocked_events(c, "factorial", 42)[-1]["detail"] == "capacity full"


# --- checkbox: the triage sweep's held unit counts box-wide ---------------

def test_triage_sweep_holds_one_unit_box_wide(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    gh = FakeGitHub(cands_by_target={
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    monkeypatch.setattr(main.triage, "running", lambda: True)
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2})))
    assert gh.claimed == []
