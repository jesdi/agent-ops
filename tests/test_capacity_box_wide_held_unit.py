"""The triage sweep's held unit must be subtracted box-wide even on a path
that `claims_paused` never touches. The locked capacity test for the sweep
(tests/test_multi_project_capacity.py::test_triage_sweep_holds_one_unit_box_wide)
goes through _claim_new, which is skipped whenever the sweep is running
(triage.pending() is True while triage.running() is True) — so it passes
even with a per-target occupancy count, for a reason unrelated to capacity
math. Resuming a woken task is not gated by claims_paused, so it is the seam
that actually exercises "the held unit is subtracted from the box, not from
one target's slice of it".
"""
from dispatcher.state import PARK_WAKE, Stage, load

from tests.test_main import FakeSessions, deps, patch_usage, patch_workspace
from tests.test_multi_project_capacity import (mk_task, two_target_cfg,
                                                wake_blocked_events)

import dispatcher.main as main


def test_woken_task_refused_when_sweep_holds_the_last_unit(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_target_cfg(tmp_path)  # capacity: 3
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 42, stage=Stage.IMPLEMENT, park=PARK_WAKE, slot=2)
    monkeypatch.setattr(main.triage, "running", lambda: True)
    sess = FakeSessions(alive={1, 2})
    main.run_pass(c, deps(sess=sess))
    saved = load(c.state_dir, "factorial", 42)
    assert saved.park == PARK_WAKE
    assert 42 not in [r[0] for r in sess.resumed]
    assert wake_blocked_events(c, "factorial", 42)[-1]["detail"] == "capacity full"
