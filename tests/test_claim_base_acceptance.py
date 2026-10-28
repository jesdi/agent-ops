"""Ticket 02: a claim reads the issue's base; a missing base waits in Ready.

Black-box through dispatcher.main.run_pass with a fake GitHub that serves issue
bodies (`issue_body(target, issue) -> str`), a fake notifier and a workspace
step faked at its boundary: it records the base it is given and raises
workspace.MissingBaseError for a base that is not on the fake origin.
"""
import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

import dispatcher.main as main
import dispatcher.workspace as workspace
from dispatcher import failures
from dispatcher.github import Candidate
from dispatcher.state import Stage, TaskState, load, save

from tests.test_main import (FakeGitHub, FakeNotifier, FakeSessions, cfg, deps,
                             patch_usage)


class BodyGitHub(FakeGitHub):
    def __init__(self, bodies, **kw):
        super().__init__(**kw)
        self.bodies = bodies  # issue -> body; edit it between passes

    def issue_body(self, target, issue):
        return self.bodies.get(issue, "")


class Origin:
    """The branches on the fake origin and the workspace calls made against it."""

    def __init__(self, monkeypatch, branches=("main",)):
        self.branches = set(branches)
        self.calls = []  # (issue, base)

        def fake_create(target, issue, base="main", dry_run=False):
            self.calls.append((issue, base))
            if base not in self.branches:
                raise workspace.MissingBaseError(f"base {base!r} is not on origin")
            wt = Path(target.worktrees_path) / f"task-{issue}"
            (wt / ".agent").mkdir(parents=True, exist_ok=True)
            return str(wt)
        monkeypatch.setattr(main, "create_workspace", fake_create)


@pytest.fixture
def box(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    return cfg(tmp_path), Origin(monkeypatch, {"main", "feat/new-cart"})


def run(c, gh, notifier=None, alive=()):
    sess = FakeSessions(alive=alive)
    n = notifier or FakeNotifier()
    main.run_pass(c, deps(gh, sess, n))
    return sess, n


def gh_for(bodies, issues=(42,)):
    return BodyGitHub(bodies, cands=[Candidate(i, f"t{i}", f"u{i}") for i in issues])


def claimed_base(c, issue=42):
    return load(c.state_dir, "portfolio_eval", issue).base


def pings(n, *needles):
    return [(t, ctx) for t, ctx in n.calls
            if all(x in repr((t, ctx)) for x in needles)]


def test_base_line_names_the_integration_branch(box):
    c, origin = box
    run(c, gh_for({42: "Swap checkout\n\nBase: feat/new-cart\n"}))
    assert origin.calls == [(42, "feat/new-cart")]
    assert claimed_base(c) == "feat/new-cart"


def test_no_base_line_is_main(box):
    c, origin = box
    run(c, gh_for({42: "Swap checkout\nBased on the old cart\n"}))
    assert origin.calls == [(42, "main")]
    assert claimed_base(c) == "main"


def test_base_line_among_other_lines_with_spaces(box):
    c, origin = box
    run(c, gh_for({42: "A paragraph.\n\nBlocked by: #40\n  Base: feat/new-cart  \n"}))
    assert claimed_base(c) == "feat/new-cart"
    assert origin.calls == [(42, "feat/new-cart")]


def test_first_of_two_base_lines_wins(box):
    c, origin = box
    origin.branches.add("feat/old-cart")
    run(c, gh_for({42: "Base: feat/new-cart\nmore text\nBase: feat/old-cart\n"}))
    assert claimed_base(c) == "feat/new-cart"


def test_explicit_main_is_main(box):
    c, origin = box
    run(c, gh_for({42: "Base: main\n"}))
    assert origin.calls == [(42, "main")]
    assert claimed_base(c) == "main"


def test_base_at_the_boundary_of_allowed_characters(box):
    c, origin = box
    origin.branches.add("feat/new-cart_v2.1")
    run(c, gh_for({42: "Base: feat/new-cart_v2.1\n"}))
    assert claimed_base(c) == "feat/new-cart_v2.1"


def test_base_edited_after_claim_does_not_change(box):
    c, origin = box
    gh = gh_for({42: "Base: feat/new-cart\n"})
    run(c, gh)
    gh.bodies[42] = "Base: main\n"
    run(c, gh, alive={42})
    assert claimed_base(c) == "feat/new-cart"


def test_task_file_without_a_base_field_loads_as_main(tmp_path):
    c = cfg(tmp_path)
    save(c.state_dir, TaskState(issue=17, target="portfolio_eval", stage=Stage.REVIEW,
                                slot=0, worktree="/w", branch="agent/task-17",
                                title="t", updated_at="2026-07-21T00:00:00+00:00"))
    p = Path(c.state_dir) / "task-portfolio_eval-17.json"
    d = json.loads(p.read_text())
    d.pop("base", None)
    p.write_text(json.dumps(d))
    assert load(c.state_dir, "portfolio_eval", 17).base == "main"


def test_missing_base_is_not_claimed_and_pings_once(box):
    c, origin = box
    origin.branches.discard("feat/new-cart")
    gh = gh_for({42: "Base: feat/new-cart\n"})
    sess, n = run(c, gh)
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert gh.claimed == [] and sess.spawned == []
    assert gh.created_issues == []
    assert not failures.quarantine_path(c.state_dir, "portfolio_eval", 42).exists()
    assert len(pings(n, "42", "feat/new-cart")) == 1


def test_missing_base_stays_quiet_on_later_passes(box):
    c, origin = box
    origin.branches.discard("feat/new-cart")
    gh = gh_for({42: "Base: feat/new-cart\n"})
    n = FakeNotifier()
    for _ in range(4):
        run(c, gh, n)
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert len(pings(n, "42", "feat/new-cart")) == 1


def test_changed_missing_base_pings_again(box):
    c, origin = box
    origin.branches.discard("feat/new-cart")
    gh = gh_for({42: "Base: feat/new-cart\n"})
    n = FakeNotifier()
    run(c, gh, n)
    gh.bodies[42] = "Base: feat/cart-v2\n"
    run(c, gh, n)
    assert len(pings(n, "42", "feat/new-cart")) == 1
    assert len(pings(n, "42", "feat/cart-v2")) == 1


def test_base_pushed_later_is_claimed(box):
    c, origin = box
    origin.branches.discard("feat/new-cart")
    gh = gh_for({42: "Base: feat/new-cart\n"})
    run(c, gh)
    origin.branches.add("feat/new-cart")
    run(c, gh)
    assert claimed_base(c) == "feat/new-cart"


def test_missing_base_does_not_hold_up_other_issues(box):
    c, origin = box
    origin.branches.discard("feat/new-cart")
    gh = gh_for({42: "Base: feat/new-cart\n", 44: "no base here\n"}, issues=(42, 44))
    run(dc_replace(c, capacity=1), gh)
    assert gh.claimed == [44]
    assert load(c.state_dir, "portfolio_eval", 42) is None


def test_invalid_branch_name_waits_and_reaches_no_command(box):
    c, origin = box
    gh = gh_for({42: "Base: feat/new-cart; rm -rf ~\n"})
    sess, n = run(c, gh)
    assert load(c.state_dir, "portfolio_eval", 42) is None
    assert gh.claimed == [] and gh.created_issues == []
    assert not failures.quarantine_path(c.state_dir, "portfolio_eval", 42).exists()
    assert len(pings(n, "42")) == 1
    assert len(pings(n, "42", "no valid branch")) == 1
    assert origin.calls == []
    assert all("rm -rf" not in repr(x) for x in (origin.calls, n.calls, sess.spawned))


def test_other_provisioning_failure_still_reports_and_quarantines(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), infra_repo="jesdi/agent-ops")
    seen = []

    def boom(target, issue, base="main", dry_run=False):
        seen.append(base)
        raise RuntimeError("disk full")
    monkeypatch.setattr(main, "create_workspace", boom)
    gh = gh_for({42: "Base: feat/new-cart\n"})
    run(c, gh)
    assert seen == ["feat/new-cart"]
    assert len(gh.created_issues) == 1
    assert failures.quarantine_path(c.state_dir, "portfolio_eval", 42).exists()
