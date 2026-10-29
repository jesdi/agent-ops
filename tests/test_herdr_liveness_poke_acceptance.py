"""Acceptance: a stopped session is poked, not parked (herdr-liveness-parking, ticket 01).

Names that do not exist yet (Poke, poked_since, pokes, now=, poke_cap=) are
reached inside each test so a missing one fails that test alone. Time is
controlled by writing `poked_since` relative to the wall clock or by passing
`now=`; nothing sleeps."""
import json
import time
from dataclasses import replace as dc_replace

import pytest

import dispatcher.main as main
from dispatcher import eventlog
from dispatcher.state import (PARK_HUMAN, Stage, StageSignal, TaskState,
                              has_waiting, load, mark_background,
                              mark_waiting, save)
from tests.test_main import (FakeSessions, Reply, cfg, deps, make_task,
                             patch_events, patch_usage, patch_workspace)

T, N = "portfolio_eval", 42
MIN, HOUR = 60, 3600
POKE = ("Check your background terminals. If the work finished, continue the stage. "
        "If it is still running, wait for it. If you need the operator, set the stage "
        "status to blocked with a note.")
STALL_NOTE = "(no session output for 10m — likely login/trust prompt or hang)"
WORK = {"id": "b1", "type": "shell", "command": "make crap-gate"}


class HerdrSessions(FakeSessions):
    """FakeSessions plus herdr's view: state[issue] = (status, counter) or None."""

    def __init__(self, state=None, **kw):
        super().__init__(**kw)
        self.state = dict(state or {})

    def agent_state(self, repository, issue):
        return self.state.get(issue)

    def stop(self, status="done", idle=30.0):
        self.state[N], self.idle[N] = (status, 5), idle

    def work(self):
        self.state[N], self.idle[N] = ("working", 6), 0.0


def _signal(wt, status="working", stage="review", **extra):
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": stage, "status": status, **extra}))


def _setup(tmp_path, monkeypatch, marker=True, stage=Stage.REVIEW, c=None, **task_kw):
    patch_usage(monkeypatch)
    c = c or cfg(tmp_path)
    wt = make_task(c, issue=N, stage=stage, **task_kw)
    _signal(wt, stage=stage.value)
    if marker:
        mark_waiting(c.state_dir, T, N)
    sess = HerdrSessions(alive=[N])
    return c, wt, sess, deps(sess=sess)


def _task(c):
    return load(c.state_dir, T, N)


def _ev(c, name):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == name]


def _poke_state(c, ago, pokes):
    t = _task(c)
    save(c.state_dir, dc_replace(t, poked_since=time.time() - ago, pokes=pokes))


# --- herdr working: never parked, never poked --------------------------------

def test_working_session_with_waiting_marker_is_neither_parked_nor_poked(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.work()
    for _ in range(2):
        main.run_pass(c, d)
    assert _task(c).park == "" and sess.ended == []
    assert sess.sent_text == [] and _ev(c, "poked") == []


# --- the poke ------------------------------------------------------------------

@pytest.mark.parametrize("status", ["done", "idle"])
def test_stopped_session_is_poked_once_with_the_line(tmp_path, monkeypatch, status):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop(status)
    main.run_pass(c, d)
    assert sess.sent_text == [(N, POKE)]
    assert "\n" not in POKE
    assert len(_ev(c, "poked")) == 1
    t = _task(c)
    assert t.park == "" and sess.ended == []
    assert has_waiting(c.state_dir, T, N)      # the marker stays: poke is safe, not consumed


def test_poked_again_while_stopped_and_not_while_working(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    main.run_pass(c, d)
    first = _task(c)
    assert first.pokes == 1 and first.poked_since > 0
    sess.work()                                  # answering the poke
    main.run_pass(c, d)
    assert len(sess.sent_text) == 1
    sess.stop()
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert len(sess.sent_text) == 3 and len(_ev(c, "poked")) == 3
    assert _task(c).park == ""


def test_pokes_without_working_pass_keep_the_clock_and_count(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    main.run_pass(c, d)
    since = _task(c).poked_since
    main.run_pass(c, d)
    t = _task(c)
    assert (t.pokes, t.poked_since) == (2, since)


# --- the cap -------------------------------------------------------------------

def test_poking_past_the_cap_parks_with_note_and_ends_session(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    _poke_state(c, 3 * HOUR + MIN, pokes=36)
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN
    assert t.park_note == "(poked 36 times over 180m — stopped mid-stage without a stage signal)"
    assert [e["detail"] for e in _ev(c, "parked")] == [t.park_note]
    assert sess.ended == [N]
    assert sess.sent_text == []


def test_poke_inside_the_cap_is_not_a_park(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    _poke_state(c, 2 * HOUR + 59 * MIN, pokes=35)
    main.run_pass(c, d)
    assert _task(c).park == "" and sess.sent_text == [(N, POKE)]


def test_working_pass_resets_the_poke_clock(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    _poke_state(c, 2 * HOUR + 50 * MIN, pokes=34)
    sess.work()
    main.run_pass(c, d)
    t = _task(c)
    assert (t.poked_since, t.pokes) == (0.0, 0)
    sess.stop()                                  # stopped again, well inside a fresh cap
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == "" and sess.sent_text == [(N, POKE)]
    assert t.pokes == 1 and abs(t.poked_since - time.time()) < 60


# --- the stage signal wins ------------------------------------------------------

def test_poked_session_that_writes_blocked_is_parked_with_its_note(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT, ticket_count=5)
    sess.stop()
    main.run_pass(c, d)
    assert len(sess.sent_text) == 1
    _signal(wt, "blocked", stage="implement", note="need the staging credentials")
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == "need the staging credentials"
    assert sess.ended == [N] and len(sess.sent_text) == 1


def test_poked_session_that_writes_awaiting_answers_is_parked(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.SPEC)
    sess.stop()
    main.run_pass(c, d)
    assert len(sess.sent_text) == 1
    (wt / ".agent" / "questionnaire.md").write_text("# Q\n")
    _signal(wt, "awaiting-answers", stage="spec", note="5 questions",
            artifact=".agent/questionnaire.md")
    main.run_pass(c, d)
    assert _task(c).park == PARK_HUMAN and len(sess.sent_text) == 1


def test_poked_session_that_writes_done_advances_as_before(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT,
                            ticket_count=2, gate_rounds=2)
    sess.stop()
    main.run_pass(c, d)
    assert len(sess.sent_text) == 1
    _signal(wt, "done", stage="implement", note="")
    main.run_pass(c, d)
    assert _task(c).stage == Stage.REVIEW
    assert sess.spawned[-1][1] == "review"


def test_blocked_signal_with_herdr_working_parks_with_the_note(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT, ticket_count=5)
    _signal(wt, "blocked", stage="implement", note="need API key")
    sess.work()
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == "need API key"
    assert sess.sent_text == []


# --- the stall (no marker) --------------------------------------------------------

@pytest.mark.parametrize("status", ["idle", "blocked"])
def test_hang_without_marker_stall_parks_byte_for_byte(tmp_path, monkeypatch, status):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, marker=False)
    sess.stop(status, idle=11 * MIN)
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == STALL_NOTE
    assert sess.sent_text == [] and sess.ended == [N]


def test_no_marker_idle_under_timeout_is_left_alone(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, marker=False)
    sess.stop("idle", idle=9 * MIN)
    main.run_pass(c, d)
    assert _task(c).park == "" and sess.sent_text == [] and sess.ended == []


# --- left alone --------------------------------------------------------------------

def test_unknown_idle_is_neither_parked_nor_poked(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.state[N] = ("done", 5)                  # idle_seconds -> None
    assert sess.idle_seconds(T, N) is None
    main.run_pass(c, d)
    assert _task(c).park == "" and sess.sent_text == [] and sess.ended == []


def test_background_marker_session_idle_two_hours_is_left_alone(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, marker=False)
    mark_background(c.state_dir, T, N, [WORK], now=time.time() - 5 * MIN)
    sess.stop("idle", idle=2 * HOUR)
    for _ in range(2):
        main.run_pass(c, d)
    assert _task(c).park == "" and sess.sent_text == [] and sess.ended == []


def test_stall_after_zero_never_parks_or_pokes(tmp_path, monkeypatch):
    c = dc_replace(cfg(tmp_path), stall_after_seconds=0)
    c, wt, sess, d = _setup(tmp_path, monkeypatch, c=c)
    sess.stop("idle", idle=3 * HOUR)
    main.run_pass(c, d)
    assert _task(c).park == "" and sess.sent_text == [] and sess.ended == []


# --- fresh poke clock ----------------------------------------------------------------

def test_park_and_resume_starts_a_fresh_poke_clock(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT, ticket_count=5)
    _poke_state(c, 3 * HOUR + 5 * MIN, pokes=37)
    _signal(wt, "blocked", stage="implement", note="need a key")
    sess.stop()
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == "need a key"
    patch_events(monkeypatch, [Reply(reply_to_msg_id=t.park_msg_id, text="use key X")])
    main.run_pass(c, d)
    patch_events(monkeypatch, [])
    assert _task(c).park == ""
    _signal(wt, "working", stage="implement")
    sess.alive_set.add(N)                        # the resumed session is live
    mark_waiting(c.state_dir, T, N)
    sess.stop()
    sess.sent_text.clear()
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == "" and sess.sent_text == [(N, POKE)]
    assert t.pokes == 1 and abs(t.poked_since - time.time()) < 60


def test_replacing_stage_launch_clears_the_poke_clock(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT,
                            ticket_count=2, gate_rounds=2)
    _poke_state(c, 90 * MIN, pokes=18)
    _signal(wt, "done", stage="implement", note="")
    sess.stop()
    main.run_pass(c, d)
    t = _task(c)
    assert t.stage == Stage.REVIEW and sess.spawned[-1][1] == "review"
    assert (t.poked_since, t.pokes) == (0.0, 0)


# --- the decision table (pass_actions) -------------------------------------------------

def _decide(*, waiting=True, idle=30.0, since=0.0, pokes=0, now=1_000_000.0,
            cap=10800, stall_after=600.0, signal="working"):
    task = TaskState(issue=N, repository=T, stage=Stage.REVIEW, slot=0, worktree="/w",
                     branch="b", title="t", updated_at="", track="standard")
    extra = {}
    if since:                                    # poke-clock inputs only where a row needs them
        task = dc_replace(task, poked_since=since, pokes=pokes)
        extra = dict(now=now, poke_cap=cap)
    sig = StageSignal(stage="review", status=signal)
    from dispatcher import machine
    return machine.pass_actions(task, sig, True, waiting, None, idle_seconds=idle,
                                stall_after=stall_after, **extra)


def test_table_waiting_marker_stopped_is_a_poke():
    from dispatcher import machine
    [act] = _decide()
    assert isinstance(act, machine.Poke) and act.text == POKE


def test_table_poke_repeats_inside_the_cap():
    from dispatcher import machine
    [act] = _decide(since=1_000_000.0 - 3 * HOUR, pokes=36)     # exactly the cap: not past it
    assert isinstance(act, machine.Poke)


def test_table_past_the_cap_parks_with_cap_note():
    from dispatcher import machine
    [act] = _decide(since=1_000_000.0 - 3 * HOUR - 1, pokes=36)
    assert isinstance(act, machine.ParkForInput)
    assert act.note == "(poked 36 times over 180m — stopped mid-stage without a stage signal)"


def test_table_herdr_working_does_nothing_even_past_the_cap():
    from dispatcher import machine
    acts = _decide(idle=0.0, since=1_000_000.0 - 4 * HOUR, pokes=48)
    assert all(isinstance(a, machine.NoOp) for a in acts)


def test_table_no_marker_idle_past_stall_parks_with_stall_note():
    from dispatcher import machine
    [act] = _decide(waiting=False, idle=11 * MIN)
    assert isinstance(act, machine.ParkForInput) and act.note == STALL_NOTE


def test_table_no_marker_never_pokes_even_with_poke_state():
    from dispatcher import machine
    acts = _decide(waiting=False, idle=60.0, since=1_000_000.0 - HOUR, pokes=12)
    assert all(isinstance(a, machine.NoOp) for a in acts)


@pytest.mark.parametrize("kw", [dict(idle=None), dict(stall_after=0)])
def test_table_unknown_idle_or_stall_zero_does_nothing(kw):
    from dispatcher import machine
    acts = _decide(**kw)
    assert all(isinstance(a, machine.NoOp) for a in acts)
