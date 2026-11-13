"""Acceptance: a running session's signals, death and silence go through the
lifecycle table (#132 ticket 04). Driven through run_pass, observed through
the task file, events.jsonl and the fakes."""
import importlib.util
import json
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import eventlog, spec_publish
from dispatcher.classify import Observation, classify
from dispatcher.lifecycle import (At, Cause, Event, EventType, FailReason, HumanReason,
                                  Death, Kind, Pane, Silence, Status, decide, legacy_fields,
                                  legacy_view, occupies)
from dispatcher.state import (NO_SLOT, BackgroundWait, Stage, StageSignal, TaskState,
                              active, load, load_all, mark_background, mark_waiting,
                              save)
from tests.test_herdr_liveness_poke_acceptance import HerdrSessions
from tests.test_main import (FakeGitHub, FakeSessions, cfg, deps, make_task,
                             patch_usage, patch_workspace)
from tests.test_lifecycle import LOOKED, report
from tests.test_session_rules_characterization import GOOD_SPEC, LOGIN_TAIL, WORK

T = "portfolio_eval"
HOUR = 3600
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def c(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr(main.spec_publish, "ensure_published",
                        lambda **kw: spec_publish.PublishResult(url="https://x/spec"))
    return cfg(tmp_path)


def task(c, issue=42, stage=Stage.IMPLEMENT, **kw):
    kw.setdefault("updated_at", NOW)
    return make_task(c, issue=issue, stage=stage, **kw)


def signal(wt, status, stage="implement", **extra):
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": stage, "status": status, **extra}))


def run(c, sess, gh=None):
    d = deps(gh or FakeGitHub(), sess)
    main.run_pass(c, d)
    return d


def transitions(c):
    return [e["transition"] for e in eventlog.read_tail(c.state_dir)
            if e["event"] == "transition"]


# --- the scenarios ---------------------------------------------------------

def test_a_crash_keeps_the_stage(c):
    task(c, issue=363)
    run(c, FakeSessions(alive=set()))
    t = load(c.state_dir, T, 363)
    assert t.at == At(Stage.IMPLEMENT, Status(Kind.FAILED, reason=FailReason.SESSION_CRASH))
    assert t.slot == NO_SLOT
    assert transitions(c) == [{"from": "running", "event": "session-died", "to": "failed",
                               "stage": "implement"}]


def test_parking_releases_the_slot(c):
    signal(task(c, slot=3), "blocked", note="which DB?")
    run(c, FakeSessions(alive={42}))
    t = load(c.state_dir, T, 42)
    assert t.at.status == Status(Kind.PARKED_HUMAN, reason=HumanReason.DECISION,
                                 note="which DB?")
    assert t.slot == NO_SLOT and not occupies(t.at.status)


def test_login_hold_keeps_the_slot(c):
    signal(task(c, stage=Stage.SPEC, slot=1), "blocked", stage="spec", note="stuck")
    d = run(c, FakeSessions(alive={42}, tail=LOGIN_TAIL))
    t = load(c.state_dir, T, 42)
    assert t.at == At(Stage.SPEC, Status(Kind.HELD_LOGIN, note="stuck"))
    assert t.slot == 1 and active(load_all(c.state_dir)) == [t]
    assert d.sessions.ended == [] and d.notifier.sent == ["needs_relogin"]


def test_a_stopped_session_is_poked_then_parked(c):
    """The poke cap is a time (background_wait_seconds, as today): two
    passes inside it poke, the pass past it parks."""
    c = dc_replace(c, background_wait_seconds=2 * HOUR)
    signal(task(c), "working")
    sess = HerdrSessions({42: ("idle", 5)}, alive=[42], idle={42: 30.0})
    for _ in range(2):
        mark_waiting(c.state_dir, T, 42)
        run(c, sess)
        assert load(c.state_dir, T, 42).at.status.kind is Kind.RUNNING
    assert len(sess.sent_text) == 2
    t = load(c.state_dir, T, 42)
    save(c.state_dir, dc_replace(t, poked_since=time.time() - 2 * HOUR - 1))
    mark_waiting(c.state_dir, T, 42)
    run(c, sess)
    status = load(c.state_dir, T, 42).at.status
    assert (status.kind, status.reason) == (Kind.PARKED_HUMAN, HumanReason.STALLED)


def test_a_background_wait_holds_the_task_running_until_its_cap(c):
    signal(task(c, stage=Stage.REVIEW), "working", stage="review")
    mark_background(c.state_dir, T, 42, [WORK], now=time.time() - 60)
    sess = HerdrSessions({42: ("idle", 5)}, alive=[42], idle={42: 3 * HOUR})
    run(c, sess)
    run(c, sess)
    assert load(c.state_dir, T, 42).at.status.kind is Kind.RUNNING
    marker = Path(c.state_dir) / f"background-{T}-42"
    wait = json.loads(marker.read_text())
    marker.write_text(json.dumps({**wait, "since": time.time() - 4 * HOUR}))
    run(c, sess)
    status = load(c.state_dir, T, 42).at.status
    assert (status.kind, status.reason) == (Kind.PARKED_HUMAN, HumanReason.STALLED)


def test_a_task_whose_track_left_the_config_is_parked_for_config(c):
    signal(task(c, track="renamed"), "working")
    run(c, FakeSessions(alive={42}))
    status = load(c.state_dir, T, 42).at.status
    assert (status.kind, status.reason) == (Kind.PARKED_HUMAN, HumanReason.CONFIG)
    assert transitions(c)[0]["event"] == "track-unconfigured"


def test_a_launch_that_raises_fails_at_the_stage_that_could_not_start(c):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="standard")
    signal(task(c, issue=43, stage=Stage.REVIEW, slot=1), "done", stage="review",
           artifact="https://github.com/jesdi/portfolio_eval/pull/9")
    gh = FakeGitHub()
    run(c, FakeSessions(alive={42, 43}, spawn_raises={42}), gh)
    t = load(c.state_dir, T, 42)
    # The legacy file stores no failure reason: crashed_stage reads back as a
    # session crash until the status is stored (ticket 06). The reason is
    # the one the transition's event decides: task-crash.
    assert (t.at.stage, t.at.status.kind) == (Stage.PLAN, Kind.FAILED)
    assert (t.spec_path, t.track) == ("spec.md", "standard")
    assert {"from": "running", "event": "launch-failed", "to": "failed",
            "stage": "plan"} in transitions(c)
    from dispatcher.lifecycle import LaunchFailure
    assert decide(At(Stage.SPEC, Status(Kind.RUNNING)), Event(
        EventType.LAUNCH_FAILED, LaunchFailure(Stage.PLAN))).status == Status(
            Kind.FAILED, reason=FailReason.TASK_CRASH)
    assert (42, "task crashed mid-pass") in gh.released
    assert "task-crash" in gh.created_issues[0][1]
    assert load(c.state_dir, T, 43).stage is Stage.PR_OPEN   # the pass went on


def test_a_dispatcher_killed_between_the_launch_and_the_commit_repeats_the_launch(
        c, monkeypatch):
    """The old status and the hand-over signal survive, so the next pass
    ends the orphan session and launches again: no running task without a
    session, and no hand-over lost."""
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="standard")

    class Killed(BaseException):
        pass

    real = main.save

    def killed(state_dir, ts):
        if ts.issue == 42 and ts.stage is Stage.PLAN:
            raise Killed
        real(state_dir, ts)

    sess = FakeSessions(alive={42})
    monkeypatch.setattr(main, "save", killed)
    with pytest.raises(Killed):
        run(c, sess)
    t = load(c.state_dir, T, 42)
    assert t.at == At(Stage.SPEC, Status(Kind.RUNNING)) and sess.spawned
    monkeypatch.setattr(main, "save", real)
    sess.ended.clear()
    run(c, sess)
    t = load(c.state_dir, T, 42)
    assert t.at == At(Stage.PLAN, Status(Kind.RUNNING)) and t.spec_path == "spec.md"
    assert sess.ended == [42] and [s[1] for s in sess.spawned] == ["plan", "plan"]
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "working"


class _Killed(BaseException):
    pass


def test_a_dispatcher_killed_after_the_commit_leaves_no_stale_done(c, monkeypatch):
    """Implement's `done` is gone before review starts: the pass after the
    kill finds review working, never a `done` that opens PR 0."""
    wt = task(c, picks={"implement": "anthropic/claude-opus-5"},
              implement_providers=["anthropic"])
    signal(wt, "done", note="all tickets")
    real = main.save

    def killed_after(state_dir, ts):
        real(state_dir, ts)
        if ts.issue == 42 and ts.stage is Stage.REVIEW:
            raise _Killed
    sess = FakeSessions(alive={42})
    monkeypatch.setattr(main, "save", killed_after)
    with pytest.raises(_Killed):
        run(c, sess)
    monkeypatch.setattr(main, "save", real)
    run(c, sess)
    t = load(c.state_dir, T, 42)
    assert t.at == At(Stage.REVIEW, Status(Kind.RUNNING)) and t.pr_number == 0
    assert [s[1] for s in sess.spawned] == ["review"]


class _Hasty(FakeSessions):
    """The new session reports before the pass that launched it is done."""
    def spawn_stage(self, repository, issue, worktree, *a, **kw):
        super().spawn_stage(repository, issue, worktree, *a, **kw)
        signal(Path(worktree), "blocked", stage="review", note="which base?")


def test_a_new_session_that_reports_at_once_keeps_its_report(c):
    wt = task(c, picks={"implement": "anthropic/claude-opus-5"},
              implement_providers=["anthropic"])
    signal(wt, "done")
    sess = _Hasty(alive={42})
    run(c, sess)
    assert json.loads((wt / ".agent" / "stage.json").read_text())["status"] == "blocked"
    run(c, sess)
    t = load(c.state_dir, T, 42)
    assert (t.at.stage, t.at.status.kind, t.at.status.note) == (
        Stage.REVIEW, Kind.PARKED_HUMAN, "which base?")


# --- one transition event per applied transition, none for an Ignore --------

def test_every_applied_transition_writes_one_transition_event(c):
    signal(task(c, issue=1, slot=0), "blocked", note="q")
    signal(task(c, issue=2, slot=1, stage=Stage.REVIEW), "working", stage="review",
           loop="review", round=1)
    task(c, issue=3, slot=2)
    run(c, FakeSessions(alive={1, 2}))
    assert sorted((e["from"], e["event"], e["to"]) for e in transitions(c)) == [
        ("running", "session-died", "failed"),
        ("running", "session-signal", "parked-human"),
        ("running", "session-signal", "running")]


@pytest.mark.parametrize("status", ["working", "pondering", "awaiting-review"])
def test_an_ignored_event_writes_no_transition_event(c, status):
    signal(task(c), status)
    before = load(c.state_dir, T, 42)
    run(c, FakeSessions(alive={42}))
    assert transitions(c) == [] and load(c.state_dir, T, 42) == before


# --- updated_at moves only with the stage or the status ----------------------

def test_a_round_counter_write_leaves_updated_at_alone(c):
    signal(task(c, stage=Stage.REVIEW), "working", stage="review", loop="review", round=1)
    before = load(c.state_dir, T, 42)
    run(c, FakeSessions(alive={42}))
    t = load(c.state_dir, T, 42)
    assert t.review_rounds == 1 and t.updated_at == before.updated_at


def test_a_poke_and_a_background_record_leave_updated_at_alone(c):
    signal(task(c), "working")
    mark_waiting(c.state_dir, T, 42)
    before = load(c.state_dir, T, 42)
    run(c, HerdrSessions({42: ("idle", 5)}, alive=[42], idle={42: 30.0}))
    poked = load(c.state_dir, T, 42)
    mark_background(c.state_dir, T, 42, [WORK], now=time.time())
    run(c, HerdrSessions({42: ("idle", 6)}, alive=[42], idle={42: 30.0}))
    recorded = load(c.state_dir, T, 42)
    assert poked.pokes == 1 and recorded.background_seq == 6
    assert before.updated_at == poked.updated_at == recorded.updated_at


def test_a_status_change_stamps_updated_at(c):
    signal(task(c), "blocked", note="q")
    before = load(c.state_dir, T, 42)
    run(c, FakeSessions(alive={42}))
    assert load(c.state_dir, T, 42).updated_at > before.updated_at


# --- precedence when two events are true in one pass -------------------------

def _seen(alive=True, waiting=False, idle=30.0, status="working", view=None, **kw):
    t = TaskState(issue=42, repository=T, stage=Stage.REVIEW, slot=0, worktree="/w",
                  branch="b", title="t", updated_at="", track="standard", **kw)
    return Observation(t, StageSignal("review", status), alive, waiting, idle, 600.0,
                       view, 1_000_000.0, 10800, look=LOOKED)


def _view(agent=("idle", 5), since=999_000.0):
    return main.BackgroundView(BackgroundWait(("b1",), since, 999_500.0), agent,
                               1_000_000.0, 10800)


def test_a_dead_session_that_left_done_takes_the_done_path():
    assert classify(_seen(alive=False, status="done")).type is EventType.SESSION_SIGNAL
    assert classify(_seen(alive=False)).type is EventType.SESSION_DIED


def test_a_waiting_marker_outranks_a_background_marker():
    assert classify(_seen(waiting=True, view=_view())).type is EventType.SESSION_IDLE


def test_a_background_wait_in_force_holds_off_the_stall():
    seen = _seen(idle=5000.0, view=_view(), background_reported=999_500.0,
                 background_seq=5)
    got = classify(seen)
    assert got.type is EventType.SESSION_WAITING
    assert decide(seen.task, got).reason == "the background wait holds"
    moved_on = seen._replace(background=_view(agent=("idle", 6)))
    assert classify(moved_on).type is EventType.SESSION_IDLE


def test_a_track_gone_outranks_everything():
    seen = _seen(alive=False, waiting=True, view=_view())._replace(gone="track 'x' gone")
    assert classify(seen).type is EventType.TRACK_UNCONFIGURED


# --- the table's session rows --------------------------------------------------

def test_a_park_at_a_login_prompt_keeps_its_round():
    from dispatcher.lifecycle import ApplyRound
    from dispatcher.loops import Decision, Loop, Outcome
    d = Decision(Outcome.EXHAUSTED, Loop.REVIEW, 3, 2)
    got = decide(At(Stage.REVIEW, Status(Kind.RUNNING)), Event(
        EventType.SESSION_SIGNAL, report(StageSignal("review", "working"), round=d,
                                         login="https://claude.ai/oauth")))
    assert got.status.kind is Kind.HELD_LOGIN and ApplyRound(d) in got.actions


def test_a_prompt_seen_before_a_poke_parks_for_the_operator():
    got = decide(At(Stage.REVIEW, Status(Kind.RUNNING)), Event(
        EventType.SESSION_IDLE, Silence(round=None, note="", pane=Pane.PROMPT, login="")))
    assert (got.status.kind, got.status.reason) == (Kind.PARKED_HUMAN, HumanReason.DECISION)


def test_a_gate_session_dead_mid_rework_with_no_rounds_parks_stalled():
    got = decide(At(Stage.PLAN, Status(Kind.RUNNING)), Event(
        EventType.SESSION_DIED, Death(at_gate=True, rounds_used=True, launches=())))
    assert (got.status.kind, got.status.reason) == (Kind.PARKED_HUMAN, HumanReason.STALLED)


@pytest.mark.parametrize("stage, status", [
    (Stage.IMPLEMENT, Status(Kind.RUNNING)),
    (Stage.PLAN, Status(Kind.HELD_PLAN_APPROVAL, path="r.html", fingerprint="fp")),
    (Stage.SPEC, Status(Kind.HELD_LOGIN, note="n")),
    (Stage.IMPLEMENT, Status(Kind.PARKED_HUMAN, reason=HumanReason.STALLED, note="n")),
    (Stage.SPEC, Status(Kind.PARKED_ANSWERS, path="q.html", fingerprint="ab", note="n")),
    (Stage.IMPLEMENT, Status(Kind.PARKED_CI, run_id=991)),
    (Stage.PLAN, Status(Kind.PARKED_REVIEW, path="r.html", fingerprint="fp", note="n")),
    (Stage.PR_OPEN, Status(Kind.PARKED_PR)),
    (Stage.REVIEW, Status(Kind.WAKING, cause=Cause.NEXT_STAGE)),
    (Stage.IMPLEMENT, Status(Kind.FAILED, reason=FailReason.SESSION_CRASH)),
])
def test_the_legacy_fields_read_back_as_the_status_they_store(stage, status):
    t = TaskState(issue=42, repository=T, stage=Stage.IMPLEMENT, slot=0, worktree="/w",
                  branch="b", title="t", updated_at="",
                  picks={"implement": "anthropic/claude-opus-5"},
                  implement_providers=["anthropic"])
    assert legacy_view(legacy_fields(t, stage, status)) == (stage, status)


@pytest.mark.parametrize("status", [Status(Kind.PARKED_ACCESS), Status(Kind.DONE),
                                    Status(Kind.WAKING, cause=Cause.REPLY)])
def test_a_status_no_session_event_enters_has_no_legacy_fields(status):
    t = TaskState(issue=42, repository=T, stage=Stage.IMPLEMENT, slot=0, worktree="/w",
                  branch="b", title="t", updated_at="")
    with pytest.raises(ValueError):
        legacy_fields(t, Stage.IMPLEMENT, status)


def test_a_failed_turn_the_table_cannot_read_is_failed_directly(c, monkeypatch):
    task(c)
    monkeypatch.setattr(main, "decide", lambda *a: (_ for _ in ()).throw(ValueError("x")))
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        main._fail_task_crash(c, deps(), c.repositories[0], load(c.state_dir, T, 42))
    t = load(c.state_dir, T, 42)
    assert (t.stage, t.crashed_stage) == (Stage.FAILED, "implement")


# --- machine.py is gone --------------------------------------------------------

def test_the_machine_module_is_gone_and_nothing_imports_it():
    assert importlib.util.find_spec("dispatcher.machine") is None
    root = Path(main.__file__).parent.parent
    sources = [p for d in ("dispatcher", "web", "telegram", "tests", "scripts")
               for p in (root / d).rglob("*.py")]
    assert not [p.name for p in sources if p != Path(__file__)
                and ("dispatcher.machine" in p.read_text() or "import machine" in p.read_text())]


# --- review waits to launch after implement ---------------------------------

def _review_waits(c, **kw):
    """Implement reported done and review was denied: waking(next-stage)."""
    wt = task(c, slot=NO_SLOT, implement_providers=["anthropic"], **kw)
    signal(wt, "done")
    assert load(c.state_dir, T, 42).at.status.cause is Cause.NEXT_STAGE


def test_review_takes_a_slot_once_admitted(c):
    _review_waits(c)
    d = run(c, FakeSessions(alive={42}))
    t = load(c.state_dir, T, 42)
    assert t.at == At(Stage.REVIEW, Status(Kind.RUNNING)) and t.slot == 0
    assert [s[1] for s in d.sessions.spawned] == ["review"]
    assert "review_started" in d.notifier.sent
    assert transitions(c) == [{"from": "waking", "event": "resume-admitted",
                               "to": "running", "stage": "review"}]


def _wake_blocked(c):
    return [e for e in eventlog.read_tail(c.state_dir) if e["event"] == "wake-blocked"]


def test_review_with_no_room_stays_waking_and_says_so_once(c):
    """Requirement 7: a wake with no capacity or slot stays waking and emits
    WakeBlocked, edge-triggered; it launches once room frees."""
    c = dc_replace(c, capacity=2)
    _review_waits(c)
    task(c, issue=1, slot=0)
    task(c, issue=2, slot=1)
    sess = FakeSessions(alive={1, 2, 42})
    for _ in range(2):
        d = run(c, sess)
        t = load(c.state_dir, T, 42)
        assert t.at == At(Stage.REVIEW, Status(Kind.WAKING, cause=Cause.NEXT_STAGE))
    assert d.sessions.spawned == []
    assert [e["detail"] for e in _wake_blocked(c)] == ["capacity full"]
    save(c.state_dir, dc_replace(load(c.state_dir, T, 2), stage=Stage.DONE, slot=NO_SLOT))
    run(c, sess)
    assert load(c.state_dir, T, 42).at == At(Stage.REVIEW, Status(Kind.RUNNING))


# --- decide runs once, on facts gathered before it -----------------------------

def _counted(monkeypatch):
    calls = []
    real = main.decide

    def counting(task, event):
        calls.append(event.type)
        return real(task, event)
    monkeypatch.setattr(main, "decide", counting)
    return calls


def test_a_poke_is_decided_once_with_the_pane_already_read(c, monkeypatch):
    signal(task(c), "working")
    mark_waiting(c.state_dir, T, 42)
    calls = _counted(monkeypatch)
    sess = HerdrSessions({42: ("idle", 5)}, alive=[42], idle={42: 30.0})
    run(c, sess)
    assert calls == [EventType.SESSION_IDLE] and len(sess.sent_text) == 1


def test_a_launch_is_decided_once_with_its_admission_already_asked(c, monkeypatch):
    wt = task(c, stage=Stage.SPEC)
    (wt / "spec.md").write_text(GOOD_SPEC)
    signal(wt, "done", stage="spec", artifact="spec.md", track="standard")
    calls = _counted(monkeypatch)
    run(c, FakeSessions(alive={42}))
    assert calls == [EventType.SESSION_SIGNAL]
    assert load(c.state_dir, T, 42).at == At(Stage.PLAN, Status(Kind.RUNNING))


def test_a_park_is_decided_once_with_the_login_prompt_already_read(c, monkeypatch):
    signal(task(c, stage=Stage.SPEC, slot=1), "blocked", stage="spec", note="stuck")
    calls = _counted(monkeypatch)
    run(c, FakeSessions(alive={42}, tail=LOGIN_TAIL))
    assert calls == [EventType.SESSION_SIGNAL]
    assert load(c.state_dir, T, 42).at.status.kind is Kind.HELD_LOGIN


@pytest.mark.parametrize("stage, at_gate", [(Stage.SPEC, False), (Stage.PLAN, False),
                                            (Stage.PLAN, True), (Stage.IMPLEMENT, False)])
@pytest.mark.parametrize("status", ["done", "awaiting-review"])
@pytest.mark.parametrize("bad_track", ["", "stage.json names track 'x'"])
@pytest.mark.parametrize("ok", [True, False])
def test_classify_asks_for_every_launch_the_rule_may_start(stage, at_gate, status,
                                                           bad_track, ok):
    """A rule that starts a launch classify did not ask about raises."""
    from dispatcher.artifacts import CheckResult, PlanRevision
    from dispatcher.classify import _launchable
    from dispatcher.loops import Retry
    from tests.test_lifecycle import PLAIN_GATE, admitted
    held = at_gate and status == "awaiting-review"
    at = At(stage, Status(Kind.HELD_PLAN_APPROVAL, path="r.html", fingerprint="fp")
            if held else Status(Kind.RUNNING))
    for skips_gate, retries in ((False, frozenset()), (True, frozenset(Retry))):
        detail = report(
            StageSignal(stage.value, status), gate=dc_replace(PLAIN_GATE, at=at_gate),
            retries=retries, bad_track=bad_track, skips_gate=skips_gate,
            tickets=CheckResult(ok, "" if ok else "bad", 2),
            revision=PlanRevision("r.html", "fp2", ""), spec=CheckResult(ok, ""),
            launches=admitted(*_launchable(stage, at_gate, status)))
        decide(at, Event(EventType.SESSION_SIGNAL, detail))


def test_the_plan_link_is_posted_only_after_the_gate_entry_commits(c, monkeypatch):
    """A comment is a notification: never for a transition that did not commit."""
    from tests.test_main import PLAN_SUMMARY, SPEC_PATH, write_tickets
    wt = task(c, stage=Stage.PLAN, spec_path=SPEC_PATH)
    write_tickets(wt, 2)
    signal(wt, "awaiting-review", stage="plan", artifact=PLAN_SUMMARY, note="plan ready")

    def killed(state_dir, ts):
        raise _Killed
    monkeypatch.setattr(main, "save", killed)
    gh = FakeGitHub()
    with pytest.raises(_Killed):
        run(c, FakeSessions(alive={42}), gh)
    assert gh.comments == []


# --- a turn saves once: the commit (and a park's message id after its notice)

def _saves(monkeypatch):
    saved = []
    real = main.save

    def counting(state_dir, ts):
        saved.append(ts)
        real(state_dir, ts)
    monkeypatch.setattr(main, "save", counting)
    return saved


def test_a_session_herdr_sees_working_ends_the_poke_stretch_in_the_commit(c, monkeypatch):
    signal(task(c, poked_since=time.time() - 60, pokes=1), "working")
    saved = _saves(monkeypatch)
    run(c, HerdrSessions({42: ("working", 6)}, alive=[42], idle={42: 0.0}))
    assert [(t.poked_since, t.pokes) for t in saved if t.issue == 42] == [(0.0, 0)]
    assert [e["event"] for e in transitions(c)] == ["session-signal"]


def test_a_park_saves_its_commit_then_only_the_message_that_asked(c, monkeypatch):
    signal(task(c), "blocked", note="which DB?")
    saved = _saves(monkeypatch)
    d = run(c, FakeSessions(alive={42}))
    commit, asked = [t for t in saved if t.issue == 42]
    assert dc_replace(asked, park_msg_id=0) == commit and asked.park_msg_id
    assert d.notifier.sent == ["parked_question"]


def test_a_wake_the_config_strands_parks_through_the_table(c):
    """_park_stranded is track-unconfigured: one transition, the park's
    question, the slot given back."""
    from dispatcher.state import PARK_WAKE
    task(c, park=PARK_WAKE, slot=NO_SLOT, track="renamed")
    d = deps(FakeGitHub(), FakeSessions())
    main._park_stranded(c, d, c.repositories[0], load(c.state_dir, T, 42), "track gone")
    t = load(c.state_dir, T, 42)
    assert (t.at.status.kind, t.at.status.reason) == (Kind.PARKED_HUMAN, HumanReason.CONFIG)
    assert transitions(c) == [{"from": "waking", "event": "track-unconfigured",
                               "to": "parked-human", "stage": "implement"}]
    assert d.notifier.sent == ["parked_question"] and t.park_msg_id
