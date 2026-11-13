"""Ticket 01 of the shared admission loop: woken tasks are admitted through
one box-wide loop. Driven through `_resume_woken(cfg, deps, admit, order,
dry_run)` only."""
import inspect
import re
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import execution_overrides
from dispatcher.state import (NO_SLOT, PARK_WAKE, SessionRecord, Stage, TaskState,
                              load, save, write_session)
from tests.test_main import (ADMIT_ALL, DENY_ALL, FakeGitHub, FakeSessions, cfg,
                             deps)
from tests.test_multi_project_capacity import (mk_task, two_repository_cfg,
                                               wake_blocked_events)


def woken(c, repository, issue, slot=NO_SLOT,
          updated_at="2026-07-21T00:00:00+00:00", **kw):
    """A woken task whose session ended a turn, so a wake resumes it."""
    mk_task(c, repository, issue, slot=slot, park=PARK_WAKE,
            updated_at=updated_at, **kw)
    write_session(c.state_dir, repository, issue,
                  SessionRecord("recorded-session", "implement"))


def resumed(d):
    return list(d.sessions.resume_calls)


def details(c, repository, issue):
    return [e["detail"] for e in wake_blocked_events(c, repository, issue)]


def test_oldest_wake_resumes_first_across_repositories(tmp_path):
    c = dc_replace(two_repository_cfg(tmp_path), capacity=1)
    woken(c, "factorial", 42, updated_at="2026-07-21T10:00:00+00:00")
    woken(c, "portfolio_eval", 3, updated_at="2026-07-21T10:05:00+00:00")
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("factorial", 42)]
    assert load(c.state_dir, "factorial", 42).slot == 0
    assert load(c.state_dir, "portfolio_eval", 3).park == PARK_WAKE
    assert details(c, "portfolio_eval", 3) == ["capacity full"]


def test_a_resume_that_fills_the_box_refuses_the_next_wake(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=2)
    mk_task(c, "portfolio_eval", 1, slot=0)
    woken(c, "portfolio_eval", 3, updated_at="2026-07-21T10:00:00+00:00")
    woken(c, "portfolio_eval", 4, updated_at="2026-07-21T10:05:00+00:00")
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3)]
    assert load(c.state_dir, "portfolio_eval", 3).slot == 1
    assert details(c, "portfolio_eval", 4) == ["capacity full"]


def test_two_slot_less_wakes_resume_in_slots_0_and_1(tmp_path):
    c = cfg(tmp_path)   # capacity 3, no slot holder
    woken(c, "portfolio_eval", 3, updated_at="2026-07-21T10:00:00+00:00")
    woken(c, "portfolio_eval", 4, updated_at="2026-07-21T10:05:00+00:00")
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3), ("portfolio_eval", 4)]
    assert [load(c.state_dir, "portfolio_eval", i).slot for i in (3, 4)] == [0, 1]


def test_no_free_slot_with_capacity_left_is_marked_once(tmp_path, monkeypatch):
    # Slot exhaustion with capacity left cannot happen with the real ceiling
    # (holds_slot == consumes_capacity, max_slots == capacity + 2): lower the
    # ceiling to 1 so the one holder takes every slot while 2 units stay free.
    monkeypatch.setattr(main, "max_slots", lambda capacity: 1)
    c = cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    woken(c, "portfolio_eval", 3)
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)
    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == []
    assert load(c.state_dir, "portfolio_eval", 3).park == PARK_WAKE
    assert details(c, "portfolio_eval", 3) == ["no free slot"]


def test_a_recorded_slot_nobody_holds_is_kept(tmp_path):
    c = cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    woken(c, "portfolio_eval", 3, slot=2)
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3)]
    assert load(c.state_dir, "portfolio_eval", 3).slot == 2


def test_a_recorded_slot_another_task_holds_is_replaced(tmp_path):
    c = cfg(tmp_path)
    mk_task(c, "portfolio_eval", 1, slot=0)
    woken(c, "portfolio_eval", 3, slot=0)
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3)]
    slot = load(c.state_dir, "portfolio_eval", 3).slot
    assert slot != 0, "resumed in slot 0, which portfolio_eval#1 holds"
    assert slot in range(main.max_slots(c.capacity))


def test_a_stranded_launch_parks_the_wake_and_resumes_nothing(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    woken(c, "portfolio_eval", 3)
    why = "no configured model can run this stage"
    monkeypatch.setattr(main, "_wake_launch", lambda *a, **kw: (None, why))
    parked = []
    real_park = main._park_stranded

    def spy(cfg_, deps_, repository, task, why_):
        parked.append((repository.name, task.issue, why_))
        return real_park(cfg_, deps_, repository, task, why_)

    monkeypatch.setattr(main, "_park_stranded", spy)
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert parked == [("portfolio_eval", 3, why)]
    assert resumed(d) == []
    assert details(c, "portfolio_eval", 3) == []


def test_a_denied_launch_waits_without_a_mark(tmp_path):
    c = cfg(tmp_path)
    woken(c, "portfolio_eval", 3)
    d = deps()

    main._resume_woken(c, d, admit=DENY_ALL, order=tuple)

    assert resumed(d) == []
    assert load(c.state_dir, "portfolio_eval", 3).park == PARK_WAKE
    assert details(c, "portfolio_eval", 3) == []


def test_a_failed_task_token_skips_without_a_mark(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    woken(c, "portfolio_eval", 3)
    monkeypatch.setattr(main, "_task_token", lambda *a, **kw: None)
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == []
    assert load(c.state_dir, "portfolio_eval", 3).park == PARK_WAKE
    assert details(c, "portfolio_eval", 3) == []


class WakingSessions(FakeSessions):
    """Resuming one task wakes another, as a reply landing mid-pass would."""

    def __init__(self, c):
        super().__init__()
        self.c = c

    def resume(self, repository, issue, *a, **kw):
        super().resume(repository, issue, *a, **kw)
        wt = self.c.repositories[1].worktrees_path + "/task-42"
        save(self.c.state_dir, TaskState(
            issue=42, repository="factorial", stage=Stage.IMPLEMENT, slot=NO_SLOT,
            worktree=wt, branch="agent/task-42", title="t",
            updated_at="2026-07-21T10:10:00+00:00", track="standard",
            park=PARK_WAKE))


def test_a_task_woken_during_the_call_waits_for_the_next(tmp_path):
    c = two_repository_cfg(tmp_path)   # capacity 3
    woken(c, "portfolio_eval", 3, updated_at="2026-07-21T10:05:00+00:00")
    write_session(c.state_dir, "factorial", 42,
                  SessionRecord("recorded-session", "implement"))
    d = deps(sess=WakingSessions(c))

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3)]
    assert load(c.state_dir, "factorial", 42).park == PARK_WAKE
    assert details(c, "factorial", 42) == []


def test_a_resume_consumes_the_stored_execution_choice(tmp_path):
    c = cfg(tmp_path)
    woken(c, "portfolio_eval", 3)
    execution_overrides.save(
        c.state_dir, "portfolio_eval", 3,
        execution_overrides.ExecutionOverride(model="claude-sonnet-4-6",
                                              bypass_usage=True))
    d = deps()

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple)

    assert resumed(d) == [("portfolio_eval", 3)]
    assert execution_overrides.load(c.state_dir, "portfolio_eval", 3) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_resume_crash_fails_only_that_task(tmp_path, capsys, dry_run):
    c = cfg(tmp_path)   # capacity 3
    woken(c, "portfolio_eval", 3, updated_at="2026-07-21T10:00:00+00:00")
    woken(c, "portfolio_eval", 4, updated_at="2026-07-21T10:05:00+00:00")
    gh = FakeGitHub()
    d = deps(gh, FakeSessions(resume_raises={3}))

    main._resume_woken(c, d, admit=ADMIT_ALL, order=tuple, dry_run=dry_run)

    assert load(c.state_dir, "portfolio_eval", 3).stage is Stage.FAILED
    assert resumed(d) == [("portfolio_eval", 4)]
    titles = [title for _repo, title, _body in gh.created_issues]
    if dry_run:
        assert titles == []
        assert "[dry-run] report task-crash failure" in capsys.readouterr().out
    else:
        assert len(titles) == 1 and "task-crash" in titles[0]


def test_resume_woken_leaves_admission_to_the_shared_loop():
    src = inspect.getsource(main._resume_woken)
    called = {name for name in ("sorted", "_oldest_first", "_box_free",
                                "allocate_slot", "_mark_wake_blocked",
                                "_park_stranded", "_task_token")
              if re.search(rf"\b{name}\(", src)}
    assert called == set(), f"_resume_woken still calls {sorted(called)}"
