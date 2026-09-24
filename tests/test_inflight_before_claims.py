"""Acceptance tests for ticket 08: in-flight work before new claims.

Black-box: drives dispatcher.main.run_pass(cfg, deps) with two targets
(portfolio_eval listed first, factorial second) and asserts on the
resulting claims/resumes/spawns and wake-blocked markers — never on
main.py internals.
"""
from dispatcher.github import Candidate
from dispatcher.state import PARK_WAKE, Stage

from tests.test_main import FakeGitHub, FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import (mk_task, two_target_cfg,
                                               wake_blocked_events)


# --- checkbox: woken work goes before new claims across targets -----------

def test_woken_task_resumes_before_other_target_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # portfolio_eval listed first, factorial second
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 42, stage=Stage.IMPLEMENT, park=PARK_WAKE, slot=2)
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
    })
    sess = FakeSessions(alive={1, 2})
    main_deps = deps(gh, sess)
    import dispatcher.main as main
    main.run_pass(c, main_deps)

    # The one free unit must go to the woken factorial task, not to a new
    # portfolio_eval claim — whatever order the targets are listed in.
    assert ("factorial", 42) in sess.resume_calls
    assert gh.claimed == []


# --- checkbox: woken tasks resume in wake order across targets ------------

def test_woken_tasks_resume_in_wake_order_across_targets(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # portfolio_eval listed first, factorial second
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "portfolio_eval", 3, stage=Stage.IMPLEMENT, park=PARK_WAKE, slot=2,
           updated_at="2026-07-21T10:05:00+00:00")
    mk_task(c, "factorial", 42, stage=Stage.IMPLEMENT, park=PARK_WAKE, slot=3,
           updated_at="2026-07-21T10:00:00+00:00")
    sess = FakeSessions(alive={1, 2})
    main_deps = deps(sess=sess)
    import dispatcher.main as main
    main.run_pass(c, main_deps)

    # The older wake (factorial, 10:00) must resume before the newer one
    # (portfolio_eval, 10:05), regardless of target listing order.
    assert ("factorial", 42) in sess.resume_calls
    assert ("portfolio_eval", 3) not in sess.resume_calls
    from dispatcher.state import load
    saved = load(c.state_dir, "portfolio_eval", 3)
    assert saved.park == PARK_WAKE
    assert wake_blocked_events(c, "portfolio_eval", 3) != []
    assert wake_blocked_events(c, "portfolio_eval", 3)[-1]["detail"] == "capacity full"


# --- checkbox: pending PR feedback goes before new claims across targets --

def test_feedback_pending_spawns_before_other_target_claims(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # portfolio_eval listed first, factorial second
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 42, stage=Stage.PR_OPEN, feedback_pending=True,
           pr_number=7, slot=2)
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
    })
    sess = FakeSessions(alive={1, 2})
    main_deps = deps(gh, sess)
    import dispatcher.main as main
    main.run_pass(c, main_deps)

    # The one free unit must go to the pending address-review, not to a
    # new portfolio_eval claim.
    assert ("factorial", 42) in sess.spawn_calls
    assert [s[1] for s in sess.spawned if s[0] == 42] == ["address-review"]
    assert gh.claimed == []
