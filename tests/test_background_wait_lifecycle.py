"""Background wait edges the acceptance suite leaves open: a crashed
session's marker, a wake between two herdr reads, and the same work
reported again after a woken turn."""
import json
import time

import dispatcher.main as main
from dispatcher import intents as intents_mod
from dispatcher.state import (PARK_HUMAN, Stage, has_waiting, load,
                              mark_background, mark_waiting, read_background)
from tests.test_main import (FakeSessions, cfg, deps, make_task, patch_usage,
                             patch_workspace)

T, N = "portfolio_eval", 42
MIN = 60
WORK = [{"id": "b1"}]
STALL = "(no session output for 10m"


class Herdr(FakeSessions):
    def __init__(self, state, **kw):
        super().__init__(**kw)
        self.state = state

    def agent_state(self, target, issue):
        return self.state


def _review_task(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=N, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "review", "status": "working"}))
    return c


def test_crash_then_resume_does_not_inherit_the_old_wait(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c = _review_task(tmp_path, monkeypatch)
    mark_background(c.state_dir, T, N, WORK, now=time.time() - MIN)
    sess = Herdr(("idle", 5), alive=[], idle={N: 11 * MIN})
    d = deps(sess=sess)
    main.run_pass(c, d)                        # the session crashed
    assert load(c.state_dir, T, N).stage is Stage.FAILED
    assert read_background(c.state_dir, T, N) is None   # the crash drops it
    intents_mod.write_intent(c.state_dir, "resume", T, N, {}, "op", 1)
    main.run_pass(c, d)                        # respawned
    sess.alive_set.add(N)                      # ...and hangs at a prompt
    for _ in range(2):
        main.run_pass(c, d)
    t = load(c.state_dir, T, N)
    assert t.park == PARK_HUMAN and t.park_note.startswith(STALL)


class WakesBetweenReads(FakeSessions):
    """herdr's first read of a pass sees the wait; any later read sees the
    session woken into a new, working turn."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reads = 0

    def _woke(self):
        self.reads += 1
        return self.reads > 1

    def agent_state(self, target, issue):
        return ("working", 6) if self._woke() else ("idle", 5)

    def idle_seconds(self, target, issue):
        return 0.0 if self._woke() else 11 * MIN


def test_a_wake_between_herdr_reads_never_stall_parks(tmp_path, monkeypatch):
    c = _review_task(tmp_path, monkeypatch)
    mark_background(c.state_dir, T, N, WORK, now=time.time() - MIN)
    main.run_pass(c, deps(sess=Herdr(("idle", 5), alive=[N], idle={N: 0.0})))
    assert load(c.state_dir, T, N).background_seq == 5
    main.run_pass(c, deps(sess=WakesBetweenReads(alive=[N])))
    assert load(c.state_dir, T, N).park == ""


CAP = "(background work still running after 180m — cap reached)"


def test_same_work_reported_after_a_woken_turn_keeps_the_cap_clock(tmp_path, monkeypatch):
    c = _review_task(tmp_path, monkeypatch)
    mark_background(c.state_dir, T, N, WORK, now=time.time() - 3 * 3600 - MIN)
    since = read_background(c.state_dir, T, N).since
    sess = Herdr(("idle", 5), alive=[N], idle={N: 0.0})
    d = deps(sess=sess)
    main.run_pass(c, d)                        # records counter 5
    sess.state = ("working", 6)                # woken into a new turn
    main.run_pass(c, d)                        # the wait is over: today's rules
    assert load(c.state_dir, T, N).park == ""
    mark_background(c.state_dir, T, N, WORK)   # the next turn end: same work
    assert read_background(c.state_dir, T, N).since == since
    sess.state = ("idle", 6)
    main.run_pass(c, d)                        # records the new report
    main.run_pass(c, d)
    t = load(c.state_dir, T, N)
    assert t.park == PARK_HUMAN and t.park_note == CAP


def test_a_waiting_ping_after_a_woken_wait_parks_mid_stage(tmp_path, monkeypatch):
    c = _review_task(tmp_path, monkeypatch)
    mark_background(c.state_dir, T, N, WORK, now=time.time() - MIN)
    sess = Herdr(("idle", 5), alive=[N], idle={N: 0.0})
    d = deps(sess=sess)
    main.run_pass(c, d)                        # records counter 5
    sess.state = ("working", 6)                # woken into a new turn...
    main.run_pass(c, d)
    mark_waiting(c.state_dir, T, N)            # ...which stops for input
    main.run_pass(c, d)
    t = load(c.state_dir, T, N)
    assert t.park == PARK_HUMAN
    assert t.park_note == "(session stopped mid-stage waiting for input)"
    assert sess.ended == [N]
    assert read_background(c.state_dir, T, N) is None
    assert not has_waiting(c.state_dir, T, N)
