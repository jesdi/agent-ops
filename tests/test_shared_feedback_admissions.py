"""Ticket 02 of the shared admission loop: feedback rounds are admitted
through the same box-wide loop as wakes. Driven through
`_spawn_feedback(cfg, deps, admit, order)`, and `run_pass` for the box-wide
marking case."""
import inspect
import re
from dataclasses import replace as dc_replace
from datetime import datetime

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides
from dispatcher.state import NO_SLOT, PARK_WAKE, Stage, load
from tests.test_main import (ADMIT_ALL, DENY_ALL, FakeSessions, cfg, deps,
                             patch_usage, patch_workspace)
from tests.test_multi_project_capacity import (mk_task, two_repository_cfg,
                                               wake_blocked_events)


def pending(c, repository, issue, slot=NO_SLOT,
            updated_at="2026-07-21T00:00:00+00:00", **kw):
    """A pr-open task whose PR got feedback, waiting for its round."""
    mk_task(c, repository, issue, stage=Stage.PR_OPEN, feedback_pending=True,
            pr_number=issue + 100, slot=slot, updated_at=updated_at, **kw)


def spawned(d):
    return list(d.sessions.spawn_calls)


def details(c, repository, issue):
    return [e["detail"] for e in wake_blocked_events(c, repository, issue)]


def test_equal_updated_at_breaks_ties_in_repository_order(tmp_path):
    c = dc_replace(two_repository_cfg(tmp_path), capacity=1)
    pending(c, "factorial", 5, updated_at="2026-07-21T10:00:00+00:00")
    pending(c, "portfolio_eval", 7, updated_at="2026-07-21T10:00:00+00:00")
    d = deps()

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert spawned(d) == [("portfolio_eval", 7)]
    assert load(c.state_dir, "portfolio_eval", 7).stage is Stage.ADDRESS_REVIEW
    assert load(c.state_dir, "factorial", 5).feedback_pending
    assert details(c, "factorial", 5) == ["capacity full"]


def test_a_full_box_marks_every_refused_wake_and_feedback(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = two_repository_cfg(tmp_path)   # capacity 3
    mk_task(c, "portfolio_eval", 1, slot=0)
    mk_task(c, "portfolio_eval", 2, slot=1)
    mk_task(c, "factorial", 9, slot=2)
    mk_task(c, "portfolio_eval", 3, park=PARK_WAKE, slot=NO_SLOT,
            updated_at="2026-07-21T10:00:00+00:00")
    mk_task(c, "factorial", 42, park=PARK_WAKE, slot=NO_SLOT,
            updated_at="2026-07-21T10:05:00+00:00")
    pending(c, "portfolio_eval", 4, updated_at="2026-07-21T10:10:00+00:00")
    pending(c, "factorial", 43, updated_at="2026-07-21T10:15:00+00:00")

    main.run_pass(c, deps(sess=FakeSessions(alive={1, 2, 9})))

    for repository, issue in (("portfolio_eval", 3), ("factorial", 42),
                              ("portfolio_eval", 4), ("factorial", 43)):
        assert details(c, repository, issue) == ["capacity full"], (repository, issue)


def test_a_recorded_slot_another_task_holds_is_replaced(tmp_path):
    c = two_repository_cfg(tmp_path)   # capacity 3
    mk_task(c, "portfolio_eval", 1, slot=0)
    pending(c, "factorial", 5, slot=0)
    d = deps()

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert spawned(d) == [("factorial", 5)]
    assert load(c.state_dir, "factorial", 5).slot == 1


def test_a_recorded_slot_nobody_holds_is_kept(tmp_path):
    c = cfg(tmp_path)   # capacity 3
    mk_task(c, "portfolio_eval", 1, slot=0)
    pending(c, "portfolio_eval", 5, slot=2)
    d = deps()

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert spawned(d) == [("portfolio_eval", 5)]
    assert load(c.state_dir, "portfolio_eval", 5).slot == 2


def test_a_stranded_launch_parks_the_feedback_and_spawns_nothing(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    pending(c, "portfolio_eval", 5)
    why = "no configured model can run this stage"
    monkeypatch.setattr(main, "_choose_launch", lambda *a, **kw: (None, False, why))
    parked = []
    real_park = main._park_stranded

    def spy(cfg_, deps_, repository, task, why_):
        parked.append((repository.name, task.issue, why_))
        return real_park(cfg_, deps_, repository, task, why_)

    monkeypatch.setattr(main, "_park_stranded", spy)
    d = deps()

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert parked == [("portfolio_eval", 5, why)]
    assert spawned(d) == []
    assert details(c, "portfolio_eval", 5) == []


def test_a_denied_launch_waits_without_a_mark(tmp_path):
    c = cfg(tmp_path)
    pending(c, "portfolio_eval", 5)
    d = deps()

    main._spawn_feedback(c, d, admit=DENY_ALL, order=tuple)

    assert spawned(d) == []
    assert load(c.state_dir, "portfolio_eval", 5).feedback_pending
    assert details(c, "portfolio_eval", 5) == []


def test_a_failed_task_token_skips_without_a_mark(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    pending(c, "portfolio_eval", 5)
    monkeypatch.setattr(main, "_task_token", lambda *a, **kw: None)
    d = deps()

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert spawned(d) == []
    assert load(c.state_dir, "portfolio_eval", 5).feedback_pending
    assert details(c, "portfolio_eval", 5) == []


class LoggingSessions(FakeSessions):
    """Logs spawn_stage into call_log beside end, so their order shows."""

    def spawn_stage(self, repository, issue, *a, **kw):
        super().spawn_stage(repository, issue, *a, **kw)
        self.call_log.append(("spawn", repository, issue))


def test_a_feedback_spawn_starts_a_fresh_round(tmp_path):
    c = cfg(tmp_path)
    pending(c, "portfolio_eval", 5)
    main._wake_blocked_path(c, "portfolio_eval", 5).touch()
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 5,
        execution_overrides.ExecutionOverride(model="claude-sonnet-4-6",
                                              bypass_usage=True))
    d = deps(sess=LoggingSessions())

    main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    log = [(kind, repo, issue) for kind, repo, issue in d.sessions.call_log
           if kind in ("end", "spawn")]
    assert log == [("end", "portfolio_eval", 5), ("spawn", "portfolio_eval", 5)]
    t = load(c.state_dir, "portfolio_eval", 5)
    assert t.stage is Stage.ADDRESS_REVIEW
    assert t.feedback_pending is False
    assert datetime.fromisoformat(t.feedback_cursor).microsecond == 0
    assert not main._wake_blocked_path(c, "portfolio_eval", 5).exists()
    assert execution_overrides.load(c.state_dir, "portfolio_eval", 5) is None


def test_a_spawn_crash_leaves_the_call_and_spawns_no_later_task(tmp_path):
    c = cfg(tmp_path)   # capacity 3
    pending(c, "portfolio_eval", 5, updated_at="2026-07-21T10:00:00+00:00")
    pending(c, "portfolio_eval", 6, updated_at="2026-07-21T10:05:00+00:00")
    d = deps(sess=FakeSessions(spawn_raises={5}))

    with pytest.raises(FileNotFoundError):
        main._spawn_feedback(c, d, admit=ADMIT_ALL, order=tuple)

    assert spawned(d) == []
    assert load(c.state_dir, "portfolio_eval", 5).stage is not Stage.FAILED
    assert load(c.state_dir, "portfolio_eval", 6).feedback_pending


def test_spawn_feedback_leaves_admission_to_the_shared_loop():
    src = inspect.getsource(main._spawn_feedback)
    called = {name for name in ("sorted", "_oldest_first", "_box_free",
                                "allocate_slot", "_mark_wake_blocked",
                                "_park_stranded", "_task_token")
              if re.search(rf"\b{name}\(", src)}
    assert called == set(), f"_spawn_feedback still calls {sorted(called)}"
