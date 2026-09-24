"""Every refused woken/feedback task is marked, not just the first hungry
one box-wide: which cards show "capacity full" must not depend on target
order or on earlier passes."""
from dispatcher.state import PARK_WAKE, Stage

from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import (mk_task, two_target_cfg,
                                               wake_blocked_events)


def _full_box(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 5, slot=2)
    return c


def _detail(c, target, issue):
    return [e["detail"] for e in wake_blocked_events(c, target, issue)]


def test_every_hungry_woken_task_is_marked(tmp_path, monkeypatch):
    c = _full_box(tmp_path, monkeypatch)
    mk_task(c, "portfolio_eval", 3, park=PARK_WAKE, slot=3,
            updated_at="2026-07-21T10:00:00+00:00")
    mk_task(c, "factorial", 42, park=PARK_WAKE, slot=4,
            updated_at="2026-07-21T10:05:00+00:00")
    import dispatcher.main as main
    main.run_pass(c, deps(sess=FakeSessions(alive={1, 2, 5})))

    assert _detail(c, "portfolio_eval", 3) == ["capacity full"]
    assert _detail(c, "factorial", 42) == ["capacity full"]


def test_every_hungry_feedback_task_is_marked(tmp_path, monkeypatch):
    c = _full_box(tmp_path, monkeypatch)
    mk_task(c, "portfolio_eval", 3, stage=Stage.PR_OPEN, feedback_pending=True,
            pr_number=6, slot=3, updated_at="2026-07-21T10:00:00+00:00")
    mk_task(c, "factorial", 42, stage=Stage.PR_OPEN, feedback_pending=True,
            pr_number=7, slot=4, updated_at="2026-07-21T10:05:00+00:00")
    import dispatcher.main as main
    main.run_pass(c, deps(sess=FakeSessions(alive={1, 2, 5})))

    assert _detail(c, "portfolio_eval", 3) == ["capacity full"]
    assert _detail(c, "factorial", 42) == ["capacity full"]
