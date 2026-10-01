"""Acceptance: the dispatcher honours a background wait (ticket 03).

New state fields and state functions are used inside each test so a missing
one fails that test alone."""
import json
import time
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog
from dispatcher.config import load_config
from dispatcher.state import (PARK_HUMAN, PARK_WAKE, Stage, TaskState,
                              has_waiting, load, mark_background,
                              mark_waiting, read_background, save)
from tests.test_main import (FakeSessions, Reply, cfg, deps, make_task,
                             patch_events, patch_usage, patch_workspace)

T, N = "portfolio_eval", 42
MIN, HOUR = 60, 3600
CAP_NOTE = "(background work still running after 180m — cap reached)"
WORK = {"id": "b1", "type": "shell", "command": "make crap-gate"}


class HerdrSessions(FakeSessions):
    """FakeSessions plus herdr's view: state[issue] = (status, counter) or None."""

    def __init__(self, state=None, **kw):
        super().__init__(**kw)
        self.state = dict(state or {})

    def agent_state(self, target, issue):
        return self.state.get(issue)


def _setup(tmp_path, monkeypatch, since_ago, idle=11 * MIN,
           tasks=(WORK,), signal="working", **kw):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=N, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "review", "status": signal}))
    mark_background(c.state_dir, T, N, list(tasks), now=time.time() - since_ago)
    sess = HerdrSessions({N: ("idle", 5)}, alive=[N], idle={N: idle}, **kw)
    return c, sess


def _passes(c, sess, n=1):
    d = deps(sess=sess)
    for _ in range(n):
        main.run_pass(c, d)
    return d


def _task(c):
    return load(c.state_dir, T, N)


def _parked_events(c):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "parked"]


def _yaml(tmp_path, extra=""):
    p = tmp_path / "targets.yaml"
    p.write_text(f"state_dir: /tmp/s\ntargets: []\n{extra}")
    return p


# --- config -----------------------------------------------------------------

def test_background_wait_seconds_defaults_to_10800(tmp_path):
    assert load_config(_yaml(tmp_path)).background_wait_seconds == 10800


def test_background_wait_seconds_configurable(tmp_path):
    p = _yaml(tmp_path, "background_wait_seconds: 7200\n")
    assert load_config(p).background_wait_seconds == 7200


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "soon", "true"])
def test_background_wait_seconds_invalid_fails_load(tmp_path, bad):
    p = _yaml(tmp_path, f"background_wait_seconds: {bad}\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_example_yaml_documents_key_next_to_stall_after_seconds():
    text = (Path(__file__).parent.parent / "targets.example.yaml").read_text()
    lines = text.splitlines()
    stall = next(i for i, l in enumerate(lines) if l.startswith("stall_after_seconds:"))
    bg = next(i for i, l in enumerate(lines) if l.startswith("background_wait_seconds:"))
    assert abs(bg - stall) <= 8


def test_context_defines_background_wait():
    text = (Path(__file__).parent.parent / "CONTEXT.md").read_text()
    assert "**Background wait**" in text


# --- the wait suppresses park and stall --------------------------------------

def test_background_wait_not_parked_nor_stalled_across_two_passes(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 11 * MIN)
    for _ in range(2):
        _passes(c, sess)
        assert _task(c).park == ""
        assert sess.ended == []


def test_wait_inside_cap_is_not_parked(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 2 * HOUR + 59 * MIN)
    _passes(c, sess, 3)
    assert _task(c).park == ""
    assert sess.ended == []


def test_wait_past_cap_parks_with_cap_note(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 3 * HOUR + MIN)
    d = _passes(c, sess)          # records the report: no park yet
    assert _task(c).park == ""
    d.notifier.sent.clear()
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == CAP_NOTE
    assert [e["detail"] for e in _parked_events(c)] == [CAP_NOTE]
    assert d.notifier.sent == ["parked_question"]
    assert CAP_NOTE in d.notifier.calls[-1][1]["note"]
    assert sess.ended == [N]
    assert read_background(c.state_dir, T, N) is None
    assert not has_waiting(c.state_dir, T, N)


def test_reply_after_cap_park_resumes_with_fresh_clock(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, sess = _setup(tmp_path, monkeypatch, 3 * HOUR + MIN)
    d = _passes(c, sess, 2)
    assert _task(c).park == PARK_HUMAN
    msg_id = _task(c).park_msg_id
    patch_events(monkeypatch, [Reply(reply_to_msg_id=msg_id, text="carry on")])
    main.run_pass(c, d)
    patch_events(monkeypatch, [])
    assert _task(c).park in ("", PARK_WAKE)
    sess.alive_set.add(N)         # the resumed session is live
    # the new session reports the SAME work 10 minutes ago
    mark_background(c.state_dir, T, N, [WORK], now=time.time() - 10 * MIN)
    for _ in range(3):
        main.run_pass(c, d)
    assert _task(c).park == ""
    assert read_background(c.state_dir, T, N) is not None


def test_new_work_restarts_the_cap_clock(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 2 * HOUR + 50 * MIN)
    mark_background(c.state_dir, T, N, [WORK, {"id": "b2", "type": "shell"}],
                    now=time.time() - 20 * MIN)
    _passes(c, sess, 3)
    assert _task(c).park == ""


def test_same_work_does_not_restart_the_cap_clock(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 3 * HOUR + 10 * MIN)
    d = _passes(c, sess)                      # records counter 5
    sess.state[N], sess.idle[N] = ("working", 6), 0.0   # a woken turn
    main.run_pass(c, d)
    sess.state[N], sess.idle[N] = ("idle", 6), 11 * MIN
    mark_background(c.state_dir, T, N, [WORK], now=time.time() - 5 * MIN)
    _passes(c, sess, 2)
    t = _task(c)
    assert t.park == PARK_HUMAN and t.park_note == CAP_NOTE


# --- the wait ends -----------------------------------------------------------

def test_counter_change_ends_wait_and_never_cap_parks(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 3 * HOUR + 30 * MIN, idle=0)
    d = _passes(c, sess)                      # records counter 5
    sess.state[N] = ("working", 6)            # the session started a new turn
    main.run_pass(c, d)
    assert read_background(c.state_dir, T, N) is not None   # kept: the wait is over, not gone
    t = _task(c)
    assert t.park == "" and t.park_note != CAP_NOTE
    assert sess.ended == []


def test_after_wait_over_idle_session_stalls_as_today(tmp_path, monkeypatch):
    c, sess = _setup(tmp_path, monkeypatch, 5 * MIN)
    d = _passes(c, sess)
    assert _task(c).park == ""
    sess.state[N] = ("idle", 6)
    main.run_pass(c, d)
    t = _task(c)
    assert t.park == PARK_HUMAN
    assert t.park_note.startswith("(no session output for 10m")
    assert read_background(c.state_dir, T, N) is None


def test_working_signal_with_waiting_marker_parks_mid_stage(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, issue=N, stage=Stage.REVIEW)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "review", "status": "working"}))
    mark_waiting(c.state_dir, T, N)
    sess = HerdrSessions({N: ("idle", 5)}, alive=[N], idle={N: 0.0})
    _passes(c, sess)
    assert _task(c).park_note == "(session stopped mid-stage waiting for input)"
    assert sess.ended == [N]


@pytest.mark.parametrize("herdr", [("working", 5), None])
def test_first_pass_with_agent_working_or_unknown_records_nothing(tmp_path, monkeypatch, herdr):
    c, sess = _setup(tmp_path, monkeypatch, 11 * MIN)
    sess.state[N] = herdr
    _passes(c, sess)
    t = _task(c)
    assert t.park == "" and sess.ended == []
    assert (t.background_reported, t.background_seq) == (0.0, 0)
    sess.state[N] = ("idle", 5)
    _passes(c, sess)
    t = _task(c)
    assert t.park == ""
    assert t.background_reported == read_background(c.state_dir, T, N).reported
    assert t.background_seq == 5


# --- task state ---------------------------------------------------------------

def test_old_task_state_file_loads_without_background_fields(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=N)
    p = next(Path(c.state_dir).glob("*42*"))
    data = json.loads(p.read_text())
    data.pop("background_reported", None)
    data.pop("background_seq", None)
    p.write_text(json.dumps(data))
    t = load(c.state_dir, T, N)
    assert (t.background_reported, t.background_seq) == (0.0, 0)


def test_background_wait_fields_round_trip(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=N)
    save(c.state_dir, TaskState(**{**_task(c).__dict__,
                                   "background_reported": 1234.5,
                                   "background_seq": 9}))
    t = _task(c)
    assert (t.background_reported, t.background_seq) == (1234.5, 9)
