"""dispatcher/drive.py: observation and stall classification, wakes and their
room (capacity, slot, wake-blocked markers), seats, and the flush of finished tasks."""
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import drive, eventlog
from dispatcher import github_app, state, task_tokens
from dispatcher import intents as intents_mod
from dispatcher.github import Candidate
from dispatcher.state import (NO_SLOT, Stage, TaskState, delete, load, load_all, save, SessionRecord, write_session)
from tests.dispatchfakes import (ADMIT_ALL, DENY_ALL, LOGIN_TAIL, FakeGitHub, FakeSessions,
                                 cfg, deps, make_task, patch_teardown, patch_usage,
                                 patch_workspace, pr_open_task, replace_capacity, _events)
from tests.statuses import RUNNING, PR, CANCELED, DONE, human, waking, failed
from dispatcher.status import Kind


def test_stalled_working_session_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"}))
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


@pytest.mark.parametrize("stage", [Stage.SPEC, Stage.IMPLEMENT, Stage.REVIEW])
def test_codex_session_stalls_after_the_shared_timeout(tmp_path, monkeypatch, stage):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=stage,
                   picks={stage.value: "openai/gpt-sol@high"})
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": stage.value, "status": "working"}))
    sess = FakeSessions(alive=[42], idle={42: 601.0})
    d = deps(sess=sess)
    sess.idle[42] = 600.0
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING
    sess.idle[42] = 601.0
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN
    assert "no session output for 10m" in t.status.note


def test_codex_stall_with_cleared_ticket_pick_still_uses_the_shared_timeout(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "models.log").write_text(
        "2026-10-08T00:00:00Z implement openai/gpt-sol@high\n")
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"}))
    assert not load(c.state_dir, "portfolio_eval", 42).picks  # the pick is cleared
    sess = FakeSessions(alive=[42], idle={42: 600.0})
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING
    sess.idle[42] = 601.0
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.PARKED_HUMAN and "no session output for 10m" in t.status.note


def test_codex_session_without_stage_signal_stalls_at_the_shared_timeout(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC, picks={"spec": "openai/gpt-sol@high"})
    sess = FakeSessions(alive=[42], idle={42: 600.0})
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING
    sess.idle[42] = 601.0
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN


def test_no_signal_stalled_session_parks(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)  # no stage.json written
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN


@pytest.mark.parametrize("picks", [
    {}, {"implement": "openai/gpt-sol@high"},
])
def test_stall_disabled_never_queries_idle(tmp_path, monkeypatch, picks):
    patch_usage(monkeypatch)
    c = dc_replace(cfg(tmp_path), stall_after_seconds=0)
    wt = make_task(c, stage=Stage.IMPLEMENT, picks=picks)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "working"}))
    sess = FakeSessions(alive=[42], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert sess.idle_queried == []
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING


def test_dead_session_never_queries_idle(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.IMPLEMENT)
    sess = FakeSessions(alive=[], idle={42: 999999.0})
    main.run_pass(c, deps(sess=sess))
    assert sess.idle_queried == []


def test_stall_with_login_tail_parks_login_and_keeps_session(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)  # no stage.json — startup login prompt
    sess = FakeSessions(alive=[42], idle={42: 999999.0}, tail=LOGIN_TAIL)
    d = deps(sess=sess)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.HELD_LOGIN
    assert t.park_msg_id == 77          # FakeNotifier.send returns 77
    assert sess.ended == []             # pane must stay ALIVE
    assert d.notifier.sent == ["needs_relogin"]
    _, ctx = d.notifier.calls[0]
    assert ctx["login_url"] == ("https://claude.ai/oauth/authorize"
                                "?code=true&client_id=abc")
    assert LOGIN_TAIL.splitlines()[0] in ctx["note"]


def test_blocked_park_with_login_tail_also_classifies(tmp_path, monkeypatch):
    # classification happens at park time regardless of what triggered it
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    wt = make_task(c, stage=Stage.IMPLEMENT)
    (wt / ".agent" / "stage.json").write_text(
        json.dumps({"stage": "implement", "status": "blocked", "note": "x"}))
    sess = FakeSessions(alive=[42], tail=LOGIN_TAIL)
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.HELD_LOGIN
    assert sess.ended == []


def test_stall_without_login_tail_stays_generic(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.SPEC)
    sess = FakeSessions(alive=[42], idle={42: 999999.0})  # default tail
    d = deps(sess=sess)
    main.run_pass(c, d)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.PARKED_HUMAN
    assert sess.ended == [42]
    assert "parked_question" in d.notifier.sent


def test_drive_wakes_ends_session_before_resuming(tmp_path, monkeypatch):
    # /attach on a login park reaches resume with the pane still LIVE;
    # _launch would type the podman command into the running claude.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=waking())
    sess = FakeSessions(alive=[42])
    main.run_pass(c, deps(sess=sess))
    assert sess.ended == [42]
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]


def test_woken_pre_router_task_resumes_on_the_untracked_track(tmp_path, monkeypatch):
    # A task parked since before tracks existed never passes _drive_task's
    # backfill: its wake must still launch, not wait forever in silence.
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=waking(), track="")
    sess = FakeSessions(alive=[42])
    main.run_pass(c, deps(sess=sess))
    assert [(issue, text.split("\n\n")[0], model, effort)
            for issue, text, model, effort in sess.resumed] == [(42, "Continue.", "anthropic/claude-opus-5", "")]


def test_woken_gate_parked_task_gets_a_fresh_slot(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "plan"))
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), slot=NO_SLOT)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING
    assert t.slot in range(3)   # a real slot, not NO_SLOT
    assert sess.resumed == [(42, "Continue.", "anthropic/claude-opus-5", "")]


def test_woken_gate_parked_task_waits_when_every_slot_is_taken(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    # capacity=1 → max_slots=3; filling all 3 slots also exhausts capacity
    # (holds_slot and consumes_capacity are the same predicate, so slot
    # exhaustion always implies capacity exhaustion with the new formula).
    c = dc_replace(cfg(tmp_path), capacity=1)
    for issue, slot in ((1, 0), (2, 1), (3, 2)):
        make_task(c, issue=issue, stage=Stage.IMPLEMENT, slot=slot)
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), slot=NO_SLOT)
    sess = FakeSessions(alive={1, 2, 3})
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.WAKING and t.slot == NO_SLOT  # still parked, retried next pass
    assert 42 not in [issue for issue, _msg, _model, _effort in sess.resumed]


def test_slot_less_back_pressure_does_not_starve_other_woken_tasks(tmp_path, monkeypatch):
    # Skipping a slot-less task must not abandon the rest of the wake queue:
    # a task that already holds a slot resumes in the same pass.
    # Issues 1 and 2 occupy slots 0 and 1; issue 43 already holds slot 2 and
    # sits second in the wake queue, behind slot-less issue 42. The invariant
    # under test is the `continue` in _drive_wakes: whatever the head of the
    # queue does, the rest of it is still processed in the same pass.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=9)
    write_session(c.state_dir, "portfolio_eval", 43, SessionRecord("recorded-session", "implement"))
    for issue, slot in ((1, 0), (2, 1)):
        make_task(c, issue=issue, stage=Stage.IMPLEMENT, slot=slot)
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), slot=NO_SLOT, updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, stage=Stage.IMPLEMENT, status=waking(), slot=2,
              updated_at="2026-07-21T00:00:01+00:00")
    sess = FakeSessions(alive={1, 2})
    main.run_pass(c, deps(sess=sess))
    assert 43 in [issue for issue, _msg, _model, _effort in sess.resumed]


def test_two_slot_less_woken_tasks_get_distinct_slots(tmp_path, monkeypatch):
    # Regression: two PARK_WAKE tasks with slot=NO_SLOT must not collide —
    # _drive_wakes re-reads load_all per iteration and saves each new slot
    # before the next iteration allocates, so both should get distinct slots.
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=9)
    make_task(c, issue=42, stage=Stage.PLAN, status=waking(), slot=NO_SLOT,
              updated_at="2026-07-21T00:00:00+00:00")
    make_task(c, issue=43, stage=Stage.PLAN, status=waking(), slot=NO_SLOT,
              updated_at="2026-07-21T00:00:01+00:00")
    main.run_pass(c, deps(sess=FakeSessions()))
    t42 = load(c.state_dir, "portfolio_eval", 42)
    t43 = load(c.state_dir, "portfolio_eval", 43)
    assert t42.slot in range(3)
    assert t43.slot in range(3)
    assert t42.slot != t43.slot


def test_flush_deletes_only_old_finished_tasks(tmp_path, monkeypatch):
    """Done, failed and won't-do all expire on terminal_at; a fresh finish
    and any unfinished task stay. Failed and canceled tasks lose their
    worktree and local branch at the flush (done ones lost it at merge)."""
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.QUEUED, status=DONE, slot=NO_SLOT)       # ancient
    make_task(c, issue=4, stage=Stage.QUEUED, status=failed("session-crash"),
              slot=NO_SLOT)                                                     # ancient
    make_task(c, issue=5, stage=Stage.QUEUED, status=CANCELED, slot=NO_SLOT)   # ancient
    fresh = datetime.now(timezone.utc).isoformat()
    make_task(c, issue=2, stage=Stage.QUEUED, status=DONE, slot=NO_SLOT, updated_at=fresh)
    make_task(c, issue=3, stage=Stage.PR_OPEN, slot=NO_SLOT, pr_number=0)
    main.run_pass(c, deps(FakeGitHub()))
    for issue in (1, 4, 5):
        assert load(c.state_dir, "portfolio_eval", issue) is None
    assert load(c.state_dir, "portfolio_eval", 2) is not None
    assert load(c.state_dir, "portfolio_eval", 3) is not None
    assert sorted(branch for _wt, branch, _dry in removed) == [
        "agent/task-1", "agent/task-4", "agent/task-5"]
    flushed = [e for e in _events(c, "transition") if e["transition"]["event"] == "flush-due"]
    assert sorted(e["issue"] for e in flushed) == [1, 4, 5]
    assert all(e["transition"]["from"] == e["transition"]["to"] for e in flushed)
    assert sorted(e["issue"] for e in _events(c, "flushed")) == [1, 4, 5]
    assert _events(c, "failed") == []      # the flush is no new failure


def test_dry_run_does_not_flush_expired_done_tasks(tmp_path, monkeypatch):
    """The flush deletes state files outright; the sandboxed state dir is
    what keeps a dry run from doing so for real."""
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.QUEUED, status=DONE, slot=NO_SLOT)   # ancient
    main.run_pass(c, deps(FakeGitHub()), dry_run=True)

    assert load(c.state_dir, "portfolio_eval", 1) is not None


def test_two_human_parks_no_longer_deadlock_a_resume(tmp_path):
    """The live 2026-08-12 failure: two PARK_HUMAN tasks plus one running
    session held every slot while capacity showed headroom, and the woken
    task was skipped every pass."""
    c = dc_replace(cfg(tmp_path), capacity=2)
    make_task(c, issue=197, slot=0, status=human())
    make_task(c, issue=267, slot=1, status=human())
    make_task(c, issue=240, slot=2, status=RUNNING)
    make_task(c, issue=198, slot=NO_SLOT, status=waking())
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    t = load(c.state_dir, "portfolio_eval", 198)
    assert t.status.kind is Kind.RUNNING and t.slot != NO_SLOT


def test_blocked_wake_emits_one_event_not_one_per_pass(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)          # holds the only capacity
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    blocked = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert len(blocked) == 1
    assert blocked[0]["issue"] == 42
    assert blocked[0]["detail"] == "capacity full"
    assert state._wake_blocked_path(c, "portfolio_eval", 42).exists()


def test_slot_exhaustion_is_reported_as_such(tmp_path, monkeypatch):
    """Practically unreachable now that max_slots == capacity + 2 (the
    capacity gate fires first), but the guard stays — and when it does fire it
    must name the resource that ran out, not just go quiet."""
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    monkeypatch.setattr(drive, "allocate_slot", lambda *a, **kw: None)
    main._drive_wakes(c, deps(), admit=ADMIT_ALL, order=tuple)
    blocked = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert [e["detail"] for e in blocked] == ["no free slot"]


def _blocked_details(c):
    return [(e["issue"], e["detail"]) for e in eventlog.read_tail(c.state_dir)
            if e["event"] == "wake-blocked"]


def _one_live_task_and_an_unreadable_file(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)       # live: holds the only unit
    (Path(c.state_dir) / "task-portfolio_eval-99.json").write_text("{not json")
    return c


def test_an_unreadable_task_file_blocks_a_wake(tmp_path):
    """The unreadable task may hold a unit and a slot no count can see: the
    wake waits, as a claim does, and says why once."""
    c = _one_live_task_and_an_unreadable_file(tmp_path)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    d = deps(sess=FakeSessions(alive={41}))
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.slot) == (Kind.WAKING, NO_SLOT)
    assert d.sessions.spawned == [] and d.sessions.resumed == []
    assert _blocked_details(c) == [(42, "unreadable task file")]


def test_an_unreadable_task_file_blocks_the_launch_of_the_next_stage(tmp_path):
    c = _one_live_task_and_an_unreadable_file(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, slot=NO_SLOT,
              status=waking(cause="next-stage"))
    d = deps(sess=FakeSessions(alive={41}))
    for _ in range(2):
        main._drive_task(c, d, c.repositories[0],
                         load(c.state_dir, "portfolio_eval", 42), ADMIT_ALL, tuple)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert (t.status.kind, t.slot) == (Kind.WAKING, NO_SLOT)
    assert d.sessions.spawned == []
    assert _blocked_details(c) == [(42, "unreadable task file")]


def test_a_launch_with_no_free_slot_waits_instead_of_aborting(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    make_task(c, issue=42, stage=Stage.REVIEW, slot=NO_SLOT,
              status=waking(cause="next-stage"))
    monkeypatch.setattr(drive, "allocate_slot", lambda *a, **kw: None)
    d = deps()
    main._drive_task(c, d, c.repositories[0],
                     load(c.state_dir, "portfolio_eval", 42), ADMIT_ALL, tuple)
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.WAKING
    assert d.sessions.spawned == []
    assert _blocked_details(c) == [(42, "no free slot")]


def test_marker_clears_once_the_wake_succeeds(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    d = deps()
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    assert state._wake_blocked_path(c, "portfolio_eval", 42).exists()
    delete(c.state_dir, "portfolio_eval", 41)                     # capacity frees up
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    assert not state._wake_blocked_path(c, "portfolio_eval", 42).exists()
    assert load(c.state_dir, "portfolio_eval", 42).status.kind is Kind.RUNNING


def test_blocked_feedback_spawn_is_reported_too(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)
    make_task(c, issue=42, slot=NO_SLOT, stage=Stage.PR_OPEN, status=waking(PR, "attention(feedback)"))
    main._drive_wakes(c, deps(), admit=ADMIT_ALL, order=tuple)
    blocked = [e for e in eventlog.read_tail(c.state_dir)
               if e["event"] == "wake-blocked"]
    assert [e["issue"] for e in blocked] == [42]


def test_kill_stops_the_task_waiting_for_a_slot_and_drops_its_marker(
        tmp_path, monkeypatch):
    """A terminal transition must not leave the wake-blocked marker behind:
    the killed card would render "waiting for a free slot" forever, and a
    later re-claim of the same issue would inherit the stale badge and the
    stale delivery_contract suffix. The marker lifecycle is healed from disk
    by the reconcile sweep, not by every terminal path remembering to unlink."""
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)      # holds the only capacity unit
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    d = deps(sess=FakeSessions(alive={41}))
    main._drive_wakes(c, d, admit=ADMIT_ALL, order=tuple)
    assert state._wake_blocked_path(c, "portfolio_eval", 42).exists()

    intents_mod.write_intent(c.state_dir, "kill", "portfolio_eval", 42, {}, "op", 1)
    main.run_pass(c, d)
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status == failed("killed")          # no longer waiting for a wake
    main.run_pass(c, d)                          # the sweep heals it from disk
    assert not state._wake_blocked_path(c, "portfolio_eval", 42).exists()


def test_a_pr_wake_blocked_on_capacity_is_marked_once_and_launches_once_it_frees(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = replace_capacity(cfg(tmp_path), 1)
    pr_open_task(c, status=waking(PR, "attention(feedback)"))
    make_task(c, issue=43)                      # holds the only capacity unit
    sess = FakeSessions(alive={43})
    main.run_pass(c, deps(sess=sess))
    main.run_pass(c, deps(sess=sess))
    assert [e["issue"] for e in _events(c, "wake-blocked")] == [42]
    assert not sess.spawned
    delete(c.state_dir, "portfolio_eval", 43)
    main.run_pass(c, deps(sess=sess))
    assert load(c.state_dir, "portfolio_eval", 42).stage is Stage.ADDRESS_REVIEW
    assert [s[:2] for s in sess.spawned] == [(42, "address-review")]


def test_drive_wakes_does_not_reset_counters(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    write_session(c.state_dir, "portfolio_eval", 42, SessionRecord("recorded-session", "implement"))
    make_task(c, stage=Stage.IMPLEMENT, status=waking(),
              review_rounds=1, gate_rounds=1, e2e_rounds=2, ci_rounds=1)
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    t = load(c.state_dir, "portfolio_eval", 42)
    assert t.status.kind is Kind.RUNNING and sess.resumed
    assert (t.review_rounds, t.gate_rounds, t.e2e_rounds, t.ci_rounds) == (1, 1, 2, 1)


def test_expired_fails_closed_on_bad_or_missing_terminal_at():
    t = TaskState(issue=1, repository="portfolio_eval", stage=Stage.QUEUED, status=DONE, slot=NO_SLOT,
                  worktree="", branch="", title="", updated_at="")
    assert not drive._expired(dc_replace(t, terminal_at=""), 0)
    assert not drive._expired(dc_replace(t, terminal_at="not-a-date"), 0)
    assert not drive._expired(dc_replace(t, stage=Stage.PR_OPEN, status=PR,
                                        terminal_at="2026-07-01T00:00:00+00:00"), 0)
    assert drive._expired(dc_replace(t, terminal_at="2026-07-01T00:00:00"), 0)  # naive = UTC


def test_flush_skips_teardown_for_a_task_whose_repository_left_the_config(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.QUEUED, status=CANCELED, slot=NO_SLOT)   # ancient
    from dispatcher.state import (delete)
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", 1),
                                 repository="gone"))
    delete(c.state_dir, "portfolio_eval", 1)
    main.run_pass(c, deps(FakeGitHub()))
    assert removed == []
    assert load(c.state_dir, "gone", 1) is None


def test_flush_skips_teardown_for_a_task_with_no_worktree(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    removed = patch_teardown(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, issue=1, stage=Stage.QUEUED, status=failed("unknown"), slot=NO_SLOT)   # ancient
    save(c.state_dir, dc_replace(load(c.state_dir, "portfolio_eval", 1), worktree=""))
    main.run_pass(c, deps(FakeGitHub()))
    assert removed == [] and load(c.state_dir, "portfolio_eval", 1) is None


# --- every wake launches through resume-admitted (ticket 09) ----------------

def _task42(c):
    return load(c.state_dir, "portfolio_eval", 42)


def test_resume_after_a_crash_respawns_the_stage(tmp_path, monkeypatch):
    from dispatcher import messages
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, slot=NO_SLOT, status=waking(failed("session-crash"), "resume"))
    messages.append(c.state_dir, "portfolio_eval", 42, "use the v2 API", "jesdi@github")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert sess.resumed == [], "a respawn never passes --continue"
    [(issue, stage, _model, prompt, _effort)] = sess.spawned
    assert (issue, stage) == (42, "implement")
    assert "session-crash" in prompt and "use the v2 API" in prompt
    t = _task42(c)
    assert (t.stage, t.status.kind) == (Stage.IMPLEMENT, Kind.RUNNING) and t.slot != NO_SLOT
    [e] = _events(c, "resumed")
    assert e["detail"] == "after crash"


def test_a_killed_task_at_plan_respawns_plan_once_admitted(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, stage=Stage.PLAN, slot=NO_SLOT, status=waking(failed("killed"), "resume"))
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    assert [s[:2] for s in sess.spawned] == [(42, "plan")] and sess.resumed == []
    assert (_task42(c).stage, _task42(c).status.kind) == (Stage.PLAN, Kind.RUNNING)


def test_reply_to_a_parked_task_continues_the_transcript(tmp_path, monkeypatch):
    from dispatcher import messages
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, slot=NO_SLOT, status=waking(human("decision", "which DB?"), "reply"))
    messages.append(c.state_dir, "portfolio_eval", 42, "yes, Postgres", "jesdi@github")
    sess = FakeSessions()
    main.run_pass(c, deps(sess=sess))
    [(issue, text, _model, _effort)] = sess.resumed
    assert issue == 42 and "yes, Postgres" in text and sess.spawned == []
    assert _task42(c).status.kind is Kind.RUNNING


def test_capacity_boundary_on_resume_writes_one_wake_blocked(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 2)
    make_task(c, issue=41, slot=0)
    make_task(c, issue=43, slot=1)
    make_task(c, slot=NO_SLOT, status=waking(failed("session-crash"), "resume"))
    sess = FakeSessions(alive={41, 43})
    for _ in range(2):
        main.run_pass(c, deps(sess=sess))
    assert _task42(c).status.kind is Kind.WAKING and sess.spawned == []
    assert [(e["issue"], e["detail"]) for e in _events(c, "wake-blocked")] == [
        (42, "capacity full")]


def test_capacity_frees_and_the_waking_task_takes_a_slot(tmp_path, monkeypatch):
    from dispatcher.status import occupies
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 2)
    make_task(c, issue=41, slot=0)
    make_task(c, slot=NO_SLOT, status=waking(failed("session-crash"), "resume"))
    main.run_pass(c, deps(sess=FakeSessions(alive={41})))
    t = _task42(c)
    assert (t.status.kind, t.slot) == (Kind.RUNNING, 1)
    assert sum(occupies(x.status) for x in load_all(c.state_dir)) == 2


def test_a_claim_that_waits_on_the_app_spawns_spec_once_admitted(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c = cfg(tmp_path)
    sess = FakeSessions()
    d = deps(FakeGitHub([Candidate(42, "Add widget", "u42")]), sess)
    real_issue = task_tokens.issue

    def app_down(*a, **kw):
        raise github_app.AppError("502")
    monkeypatch.setattr(task_tokens, "issue", app_down)
    main.run_pass(c, d)
    monkeypatch.setattr(task_tokens, "issue", real_issue)
    t = _task42(c)
    assert (t.stage, t.status.kind, t.status.cause) == (
        Stage.QUEUED, Kind.WAKING, waking(cause="launch").cause)
    assert sess.spawned == []
    main.run_pass(c, d)
    assert [s[:2] for s in sess.spawned] == [(42, "spec")]
    assert (_task42(c).stage, _task42(c).status.kind) == (Stage.SPEC, Kind.RUNNING)


def test_implement_done_waits_for_the_review_launch(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = replace_capacity(cfg(tmp_path), 1)
    wt = make_task(c, ticket_count=1)
    (wt / ".agent" / "stage.json").write_text(json.dumps({"stage": "implement", "status": "done"}))
    sess = FakeSessions(alive={42})
    deny = lambda usages, model, now, pace: DENY_ALL(model)  # noqa: E731
    monkeypatch.setattr(main, "admits", deny)
    main.run_pass(c, deps(sess=sess))
    t = _task42(c)
    assert (t.stage, t.status, t.slot) == (Stage.REVIEW, waking(cause="next-stage"), NO_SLOT)
    monkeypatch.setattr(main, "admits", lambda usages, model, now, pace: ADMIT_ALL(model))
    main.run_pass(c, deps(sess=sess))
    assert [s[:2] for s in sess.spawned] == [(42, "review")]
    assert (_task42(c).stage, _task42(c).status.kind) == (Stage.REVIEW, Kind.RUNNING)


@pytest.mark.parametrize("status,raises", [
    (waking(failed("session-crash"), "resume"), "spawn_raises"),
    (waking(human("decision", "which DB?"), "reply"), "resume_raises")])
def test_a_waking_task_whose_launch_raises_fails_at_its_stage(tmp_path, monkeypatch,
                                                              status, raises):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    make_task(c, slot=NO_SLOT, status=status)
    main.run_pass(c, deps(sess=FakeSessions(**{raises: {42}})))
    t = _task42(c)
    assert (t.stage, t.status) == (Stage.IMPLEMENT, failed("task-crash"))
    assert t.slot == NO_SLOT
