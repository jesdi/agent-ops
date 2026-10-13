"""Poke edges the acceptance file does not reach (herdr-liveness-parking, ticket 01):
the production idle computation, an undelivered poke, a dry run, a login prompt,
and each place that ends the poke clock."""
import time
from dataclasses import replace
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher.main import STOPPED_NOTE
from dispatcher.sessions import Sessions
from dispatcher.state import (PARK_HUMAN, PARK_LOGIN, SessionRecord, Stage,
                              mark_background, mark_waiting, save, write_session)
from tests.test_herdr_liveness_poke_acceptance import (HOUR, MIN, N, POKE, T,
                                                       HerdrSessions, _ev, _poke_state,
                                                       _setup, _signal, _task)
from tests.test_main import (LOGIN_TAIL, Reply, deps, patch_events,
                             patch_workspace)


class Undelivered(HerdrSessions):
    def send_text(self, target, issue, text):
        return False


class RealIdle(Sessions):
    """The production Sessions (so the production idle number) over a faked
    herdr: only what touches herdr is replaced."""

    def __init__(self, state_dir, status, seq):
        super().__init__(state_dir=state_dir)
        self.state = {N: (status, seq)}
        self.sent_text, self.ended = [], []

    def is_alive(self, target, issue):
        return True

    def agent_state(self, target, issue):
        return self.state.get(issue)

    def capture_tail(self, target, issue, lines=25):
        return "…pane tail…"

    def send_text(self, target, issue, text):
        self.sent_text.append((issue, text))
        return True

    def end(self, target, issue):
        self.ended.append(issue)


def _real(tmp_path, monkeypatch, status="done", seq=5):
    c, wt, _, _ = _setup(tmp_path, monkeypatch)
    sess = RealIdle(c.state_dir, status, seq)
    return c, wt, sess, deps(sess=sess)


def test_undelivered_poke_starts_the_clock_and_is_not_counted(tmp_path, monkeypatch):
    c, wt, _, _ = _setup(tmp_path, monkeypatch)
    sess = Undelivered(alive=[N])
    sess.stop()
    main.run_pass(c, deps(sess=sess))
    t = _task(c)
    assert (t.pokes, t.park) == (0, "") and abs(t.poked_since - time.time()) < 60
    assert _ev(c, "poked") == [] and sess.ended == []


def test_pane_that_refuses_every_poke_is_parked_at_the_cap(tmp_path, monkeypatch):
    c, wt, _, _ = _setup(tmp_path, monkeypatch)
    sess = Undelivered(alive=[N])
    sess.stop()
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = _task(c)
    assert t.poked_since > 0                     # the attempt started the clock
    save(c.state_dir, replace(t, poked_since=t.poked_since - 3 * HOUR - MIN))
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and sess.ended == [N]
    assert t.park_note == "(poked 0 times over 180m — stopped mid-stage without a stage signal)"


def test_dry_run_types_nothing_and_saves_nothing(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    main.run_pass(c, d, dry_run=True)
    t = _task(c)
    assert sess.sent_text == [] and (t.pokes, t.poked_since) == (0, 0.0)


# --- C1: production idle, fake herdr status ------------------------------------

def test_first_stopped_pass_pokes_with_the_real_idle(tmp_path, monkeypatch):
    c, wt, sess, d = _real(tmp_path, monkeypatch)
    main.run_pass(c, d)
    assert sess.sent_text == [(N, POKE)]


def test_new_counter_while_stopped_pokes_again_and_keeps_the_clock(tmp_path, monkeypatch):
    c, wt, sess, d = _real(tmp_path, monkeypatch)
    main.run_pass(c, d)
    since = _task(c).poked_since
    sess.state[N] = ("done", 6)               # answered the poke with a short turn
    main.run_pass(c, d)
    t = _task(c)
    assert len(sess.sent_text) == 2 and (t.pokes, t.poked_since) == (2, since)


def test_old_clock_and_new_counter_cap_parks(tmp_path, monkeypatch):
    c, wt, sess, d = _real(tmp_path, monkeypatch)
    _poke_state(c, 3 * HOUR + MIN, pokes=36)
    sess.state[N] = ("done", 7)
    main.run_pass(c, d)
    assert _task(c).park == PARK_HUMAN and sess.sent_text == []


# --- I1: a login park ends the stretch -------------------------------------------

def test_login_park_starts_a_fresh_poke_clock(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, wt, sess, d = _setup(tmp_path, monkeypatch, stage=Stage.IMPLEMENT, ticket_count=5)
    _poke_state(c, 4 * HOUR, pokes=36)
    _signal(wt, "blocked", stage="implement", note="needs login")
    sess.tail = LOGIN_TAIL
    sess.stop()
    main.run_pass(c, d)
    assert _task(c).park == PARK_LOGIN
    patch_events(monkeypatch, [Reply(reply_to_msg_id=_task(c).park_msg_id, text="code")])
    main.run_pass(c, d)
    patch_events(monkeypatch, [])
    assert _task(c).park == ""
    sess.tail = "…pane tail…"
    _signal(wt, "working", stage="implement")
    mark_waiting(c.state_dir, T, N)
    sess.stop()
    sess.sent_text.clear()
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == "" and sess.sent_text == [(N, POKE)] and t.pokes == 1


# --- I2: recording a background wait ends the stretch ----------------------------

def test_recording_a_background_wait_starts_a_fresh_poke_clock(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch, marker=False)
    _poke_state(c, 4 * HOUR, pokes=36)
    mark_background(c.state_dir, T, N, [{"id": "b1", "type": "shell", "command": "make"}],
                    now=time.time() - 5 * MIN)
    sess.stop()
    main.run_pass(c, d)                        # the wait is recorded
    assert _task(c).pokes == 0 and sess.sent_text == []
    mark_waiting(c.state_dir, T, N)            # hours later the session stops
    sess.state[N] = ("done", 6)
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == "" and sess.sent_text == [(N, POKE)] and t.pokes == 1


# --- the check right before the text is typed ---------------------------------------
# The idle number says "stopped" (the machine returns a Poke); herdr and the pane
# are read again in _on_poke, and that newer read decides.

def test_login_prompt_is_parked_not_poked(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.tail = LOGIN_TAIL
    sess.stop()
    main.run_pass(c, d)
    t = _task(c)
    assert sess.sent_text == [] and (t.park, t.park_note) == (PARK_LOGIN, STOPPED_NOTE)
    assert sess.ended == []                      # the login park keeps the pane


def test_old_waiting_marker_and_herdr_blocked_parks_and_types_nothing(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop("blocked")                        # a permission or trust prompt
    main.run_pass(c, d)
    t = _task(c)
    assert sess.sent_text == [] and _ev(c, "poked") == []
    assert (t.park, t.park_note) == (PARK_HUMAN, STOPPED_NOTE) and sess.ended == [N]


def test_past_the_cap_the_cap_note_wins_at_a_prompt(tmp_path, monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    _poke_state(c, 3 * HOUR + MIN, pokes=36)
    sess.stop("blocked")
    main.run_pass(c, d)
    assert _task(c).park_note.startswith("(poked 36 times") and sess.sent_text == []


def _unreadable_herdr(sess):
    del sess.state[N]


def _unreadable_pane(sess):
    sess.tail = ""


@pytest.mark.parametrize("fail", [_unreadable_herdr, _unreadable_pane])
def test_unknown_at_the_moment_of_the_poke_holds(tmp_path, monkeypatch, fail):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()
    _poke_state(c, HOUR, pokes=12)
    before = _task(c)
    fail(sess)
    main.run_pass(c, d)
    t = _task(c)
    assert sess.sent_text == [] and sess.ended == [] and t.park == ""
    assert (t.poked_since, t.pokes) == (before.poked_since, 12)


def test_turn_started_after_the_idle_read_is_not_poked_and_clears_the_clock(tmp_path,
                                                                           monkeypatch):
    c, wt, sess, d = _setup(tmp_path, monkeypatch)
    sess.stop()                                  # the idle read: stopped for 30s
    _poke_state(c, HOUR, pokes=12)
    sess.state[N] = ("working", 6)               # the read before the text: a new turn
    main.run_pass(c, d)
    t = _task(c)
    assert sess.sent_text == [] and sess.ended == [] and t.park == ""
    assert (t.poked_since, t.pokes) == (0.0, 0)


# --- a session start ends the stretch ------------------------------------------------

def test_resume_clears_the_poke_clock(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, wt, sess, d = _setup(tmp_path, monkeypatch, marker=False, stage=Stage.IMPLEMENT,
                            ticket_count=5, park=PARK_HUMAN, park_msg_id=88,
                            poked_since=time.time() - HOUR, pokes=12)
    write_session(c.state_dir, T, N, SessionRecord("recorded-session", "implement"))
    patch_events(monkeypatch, [Reply(reply_to_msg_id=88, text="go on")])
    main.run_pass(c, d)
    patch_events(monkeypatch, [])
    t = _task(c)
    assert sess.resumed and t.park == "" and (t.poked_since, t.pokes) == (0.0, 0)

