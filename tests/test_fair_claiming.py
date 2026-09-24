"""Acceptance tests for ticket 09: fair claiming across projects.

Black-box: drives dispatcher.main.run_pass(cfg, deps) with two targets
(portfolio_eval, factorial) and asserts on claim order / count via
FakeGitHub.claimed and FakeSessions.spawn_calls — never on main.py
internals.
"""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import dispatcher.main as main
from dispatcher.github import Candidate

from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import mk_task, two_target_cfg


def append_claimed(c, target, issue, ts):
    """Write a `claimed` row into events.jsonl the way eventlog.append_event
    does, but with a caller-chosen ts, for setting up "last claimed at"."""
    p = Path(c.state_dir) / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": ts, "event": "claimed", "target": target,
                       "issue": issue, "stage": "queued", "model": "",
                       "actor": "dispatcher", "detail": ""})
    with p.open("a") as fh:
        fh.write(line + "\n")


# --- checkbox: the free unit goes to the target with fewer active tasks ---

def test_free_unit_goes_to_target_with_fewer_active_tasks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # portfolio_eval listed first
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    from tests.test_main import FakeGitHub
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2})))
    assert gh.claimed == [20], "the one free unit should go to factorial (0 active < 2 active)"


# --- checkbox: an empty box alternates between targets -------------------

def test_empty_box_alternates_by_least_recently_claimed(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # portfolio_eval listed first
    append_claimed(c, "portfolio_eval", 900, "2026-07-21T09:00:00+00:00")
    append_claimed(c, "factorial", 800, "2026-07-21T08:00:00+00:00")
    from tests.test_main import FakeGitHub
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(n, f"PE {n}", f"u{n}") for n in (101, 102, 103)],
        "factorial": [Candidate(n, f"F {n}", f"u{n}") for n in (201, 202, 203)],
    })
    sess = FakeSessions(alive=set())
    main.run_pass(c, deps(gh, sess))
    order = [t for t, _issue in sess.spawn_calls]
    assert order == ["factorial", "portfolio_eval", "factorial"], (
        f"claim order should alternate starting with the least-recently-claimed "
        f"target (factorial, 08:00 < 09:00), got {order}")


# --- checkbox: a target with no candidates yields its share ---------------

def test_target_with_no_candidates_yields_its_share(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    from tests.test_main import FakeGitHub
    gh = FakeGitHub(cands_by_target={
        "factorial": [],
        "portfolio_eval": [Candidate(n, f"PE {n}", f"u{n}") for n in (101, 102, 103)],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive=set())))
    assert gh.claimed == [101, 102, 103], "all 3 units should go to portfolio_eval"


# --- checkbox: max_active boundary -----------------------------------------

def test_max_active_boundary(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    from tests.test_main import FakeGitHub

    # 1 active, max_active=2 -> exactly one more claim, even with free box
    # capacity and 3 candidates.
    c = two_target_cfg(tmp_path)
    c = dc_replace(c, targets=[dc_replace(c.targets[0], max_active=2), c.targets[1]])
    mk_task(c, "portfolio_eval", 1, slot=0)
    gh = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(n, f"PE {n}", f"u{n}") for n in (101, 102, 103)],
        "factorial": [],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1})))
    assert gh.claimed == [101], "max_active=2 with 1 active allows exactly one more claim"

    # 2 active, max_active=2 -> no claims for portfolio_eval, despite free
    # box capacity and candidates.
    c2 = two_target_cfg(tmp_path)
    c2 = dc_replace(c2, state_dir=str(tmp_path / "state2"),
                    targets=[dc_replace(c2.targets[0], max_active=2), c2.targets[1]])
    mk_task(c2, "portfolio_eval", 1, slot=0)
    mk_task(c2, "portfolio_eval", 2, slot=1)
    gh2 = FakeGitHub(cands_by_target={
        "portfolio_eval": [Candidate(n, f"PE {n}", f"u{n}") for n in (201, 202, 203)],
        "factorial": [],
    })
    main.run_pass(c2, deps(gh2, FakeSessions(alive={1, 2})))
    assert gh2.claimed == [], "max_active=2 already reached at 2 active -> no more claims"


# --- checkbox: provisioning failure removes only that target for the pass -

def test_provisioning_failure_scoped_to_target_other_target_still_claims(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = two_target_cfg(tmp_path)
    from tests.test_main import FakeGitHub
    gh = FakeGitHub(cands_by_target={
        "factorial": [Candidate(20, "Bad", "u20"), Candidate(21, "Also good", "u21")],
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
    })

    def failing_workspace(target, issue, dry_run=False):
        if target.name == "factorial":
            raise RuntimeError("podman down")
        wt = Path(target.worktrees_path) / f"task-{issue}"
        (wt / ".agent").mkdir(parents=True, exist_ok=True)
        return str(wt)

    monkeypatch.setattr(main, "create_workspace", failing_workspace)
    main.run_pass(c, deps(gh, FakeSessions(alive=set())))

    assert len(gh.created_issues) == 1, "exactly one provisioning failure reported"
    assert gh.claimed == [10], "portfolio_eval still claims in the same pass"


# --- checkbox (extra, from ticket text): a target's rank command runs only
# when it gets a turn --------------------------------------------------

def test_candidates_not_called_for_target_that_never_gets_a_turn(tmp_path, monkeypatch):
    """With capacity reached before factorial's turn, factorial.candidates()
    is never called (its rank_cmd is never run)."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    from tests.test_main import FakeGitHub

    class CountingGitHub(FakeGitHub):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.candidates_calls = []

        def candidates(self, target):
            self.candidates_calls.append(target.name)
            return super().candidates(target)

    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "portfolio_eval", 3, slot=2)  # box already full: capacity 3
    gh = CountingGitHub(cands_by_target={
        "portfolio_eval": [Candidate(10, "PE candidate", "u10")],
        "factorial": [Candidate(20, "F candidate", "u20")],
    })
    main.run_pass(c, deps(gh, FakeSessions(alive={1, 2, 3})))
    assert gh.claimed == []
    assert "factorial" not in gh.candidates_calls, (
        "factorial never gets a turn when the box is already full, so its "
        "rank_cmd (candidates()) should never run")
