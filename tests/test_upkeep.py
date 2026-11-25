import json
import os
import socket
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dispatcher.main as main
from dispatcher import state, upkeep
from dispatcher.state import NO_SLOT, Stage
from tests.usagefakes import session_usage
from tests.dispatchfakes import _ago, cfg, deps, make_task, patch_usage, patch_workspace
from tests.statuses import PR, RUNNING, human, waking, failed


def test_prune_snapshots_removes_only_old_finished(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # old + finished (PR_OPEN is not in IN_FLIGHT_STAGES) -> pruned
    make_task(c, issue=41, stage=Stage.PR_OPEN)
    (snaps / "task-41.txt").write_text("done long ago")
    os.utime(snaps / "task-41.txt", (old, old))
    # old + absent state file -> pruned
    (snaps / "task-40.txt").write_text("state gone")
    os.utime(snaps / "task-40.txt", (old, old))
    # old + still in flight (parked implement) -> kept
    make_task(c, issue=42, status=human())
    (snaps / "task-42.txt").write_text("still parked")
    os.utime(snaps / "task-42.txt", (old, old))
    # fresh + finished -> kept (within the 7-day window)
    make_task(c, issue=43, stage=Stage.QUEUED, status=failed("unknown"))
    (snaps / "task-43.txt").write_text("just failed")

    upkeep._prune_snapshots(c)

    assert not (snaps / "task-41.txt").exists()
    assert not (snaps / "task-40.txt").exists()
    assert (snaps / "task-42.txt").exists()
    assert (snaps / "task-43.txt").exists()


def test_prune_snapshots_corrupt_state_file_does_not_abort_sweep(tmp_path, monkeypatch):
    """A corrupt state file must not raise out of _prune_snapshots; the sweep
    continues and still prunes any other eligible snapshots."""
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    # corrupt state file — write invalid JSON directly
    Path(c.state_dir, "task-50.json").write_text("{not json")
    (snaps / "task-50.txt").write_text("corrupt task snap")
    os.utime(snaps / "task-50.txt", (old, old))
    # separate old+finished snapshot in the same dir (absent state file) -> must still be pruned
    (snaps / "task-51.txt").write_text("old finished")
    os.utime(snaps / "task-51.txt", (old, old))

    # must not raise
    upkeep._prune_snapshots(c)

    # sweep continued past the corrupt file and pruned the eligible one
    assert not (snaps / "task-51.txt").exists()


def test_prune_snapshots_skips_the_sweep_when_state_cannot_be_read(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    snaps = Path(c.state_dir) / "snapshots"
    snaps.mkdir(parents=True)
    old = time.time() - 8 * 24 * 3600
    (snaps / "task-52.txt").write_text("maybe still in flight")
    os.utime(snaps / "task-52.txt", (old, old))

    def unreadable(_state_dir):
        raise OSError("state dir unreadable")
    monkeypatch.setattr(upkeep, "load_all", unreadable)

    upkeep._prune_snapshots(c)

    assert (snaps / "task-52.txt").exists()


def patch_usage_dark(monkeypatch):
    monkeypatch.setattr(main, "fetch_all",
                        lambda cfg, **k: {"anthropic": session_usage(0.0, 0.0, source="unavailable")})


def dark_marker(c):
    return Path(c.state_dir) / "auth-dark"


def test_auth_dark_first_pass_marks_but_does_not_alert(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_silent_during_grace_and_since_survives(tmp_path, monkeypatch):
    """Second pass inside the grace window must not alert and must not
    rewrite 'since' — exercises the _now()->fromisoformat round-trip."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                        # pass 1: creates marker
    since_1 = json.loads(dark_marker(c).read_text())["since"]
    main.run_pass(c, d)                        # pass 2: still within grace
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert st["since"] == since_1              # since not rewritten
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_alerts_once_after_grace(tmp_path, monkeypatch):
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)                       # creates the marker
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=upkeep.AUTH_DARK_GRACE_MINUTES + 1)).isoformat()
    dark_marker(c).write_text(json.dumps({"since": old, "alerted": False}))
    main.run_pass(c, d)                       # past grace -> alert
    main.run_pass(c, d)                       # already alerted -> silent
    assert d.notifier.sent.count("auth_dark") == 1
    assert json.loads(dark_marker(c).read_text())["alerted"] is True


def test_auth_dark_clears_on_recovery(tmp_path, monkeypatch):
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    patch_usage_dark(monkeypatch)
    main.run_pass(c, d)
    assert dark_marker(c).exists()
    patch_usage(monkeypatch, util=0.2)        # usage readable again
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_budget_full_with_live_source_never_touches_auth_dark(
        tmp_path, monkeypatch):
    patch_usage(monkeypatch, util=0.95)       # full window, oauth source
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent


def test_unavailable_usage_sends_no_budget_stall(tmp_path, monkeypatch):
    """An auth outage fail-safes to no-spawns, but it is not a budget stall:
    "usage window exhausted; stalled until reset" would send the operator to
    wait out an outage only a re-login ends, 30 minutes before the accurate
    auth_dark alert. auth-dark owns the unavailable case."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    main.run_pass(c, d)
    assert "budget_stall" not in d.notifier.sent
    assert not (Path(c.state_dir) / "budget-stalled").exists()


def test_full_window_with_readable_usage_still_sends_budget_stall(
        tmp_path, monkeypatch):
    """Guard on the other side of the suppression: a genuinely exhausted
    window with a live source keeps its stall ping (and its resume)."""
    patch_usage(monkeypatch, util=0.95)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_stall") == 1
    assert (Path(c.state_dir) / "budget-stalled").exists()
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    assert d.notifier.sent.count("budget_resume") == 1


def test_auth_dark_corrupt_marker_is_repaired(tmp_path, monkeypatch):
    """Non-JSON marker must not crash the pass; it is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text("not valid json{{")
    main.run_pass(c, d)                        # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_malformed_marker_is_repaired(tmp_path, monkeypatch):
    """Parseable-but-malformed JSON (null, missing 'since') must not crash the
    pass; the marker is reset to fresh state."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    Path(c.state_dir).mkdir(parents=True, exist_ok=True)
    dark_marker(c).write_text(json.dumps(None))   # valid JSON, non-dict
    main.run_pass(c, d)                            # must not raise
    st = json.loads(dark_marker(c).read_text())
    assert st["alerted"] is False
    assert "auth_dark" not in d.notifier.sent


def _realert_h(**extra):
    return timedelta(hours=upkeep.AUTH_DARK_REALERT_HOURS, **extra)


def _dark_pass(tmp_path, monkeypatch, marker=None):
    """Dark pass(es) setup: returns (cfg, deps) with `marker` (dict or raw
    str) written to the auth-dark file when given."""
    patch_usage_dark(monkeypatch)
    patch_workspace(monkeypatch, tmp_path)
    c, d = cfg(tmp_path), deps()
    if marker is not None:
        Path(c.state_dir).mkdir(parents=True, exist_ok=True)
        dark_marker(c).write_text(
            marker if isinstance(marker, str) else json.dumps(marker))
    return c, d


def _marker(c):
    return json.loads(dark_marker(c).read_text())


def test_auth_dark_realert_hours_is_eight():
    assert upkeep.AUTH_DARK_REALERT_HOURS == 8


def test_auth_dark_silent_at_29_minutes(tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch,
                      {"since": _ago(minutes=29), "alerted": False})
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c)["alerted"] is False


def test_auth_dark_stray_last_alert_does_not_skip_grace(tmp_path, monkeypatch):
    m = {"since": _ago(minutes=10), "alerted": False, "last_alert": "garbage"}
    c, d = _dark_pass(tmp_path, monkeypatch, m)
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c) == m


def test_auth_dark_first_alert_records_last_alert(tmp_path, monkeypatch):
    since = _ago(minutes=upkeep.AUTH_DARK_GRACE_MINUTES + 1)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": False})
    main.run_pass(c, d)
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since and st["alerted"] is True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_realerts_after_interval(tmp_path, monkeypatch):
    since = _ago(hours=9)
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": since, "alerted": True,
        "last_alert": (datetime.now(timezone.utc) - _realert_h(minutes=1)).isoformat()})
    main.run_pass(c, d)
    main.run_pass(c, d)                        # back to back: silent
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since and st["alerted"] is True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_realerts_exactly_at_interval(tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(hours=9), "alerted": True,
        "last_alert": (datetime.now(timezone.utc) - _realert_h()).isoformat()})
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1


def test_auth_dark_silent_just_inside_interval(tmp_path, monkeypatch):
    m = {"since": (datetime.now(timezone.utc) - _realert_h()).isoformat(),
         "alerted": True,
         "last_alert": (datetime.now(timezone.utc) - _realert_h()
                        + timedelta(minutes=1)).isoformat()}
    c, d = _dark_pass(tmp_path, monkeypatch, m)
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c) == m


def test_auth_dark_long_outage_keeps_reminding(tmp_path, monkeypatch):
    def stale():
        return (datetime.now(timezone.utc) - _realert_h(minutes=1)).isoformat()
    c, d = _dark_pass(tmp_path, monkeypatch,
                      {"since": _ago(hours=21), "alerted": True,
                       "last_alert": stale()})
    main.run_pass(c, d)
    st = _marker(c)
    st["last_alert"] = stale()
    dark_marker(c).write_text(json.dumps(st))
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 2


def test_auth_dark_realert_reports_minutes_since_incident_began(
        tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(minutes=540), "alerted": True,
        "last_alert": _ago(minutes=481)})
    main.run_pass(c, d)
    sent = [ctx for t, ctx in d.notifier.calls if t == "auth_dark"]
    assert len(sent) == 1
    assert abs(sent[0]["minutes"] - 540) <= 1
    assert sent[0]["host"] == socket.gethostname()


def test_auth_dark_new_incident_after_recovery_starts_with_grace(
        tmp_path, monkeypatch):
    c, d = _dark_pass(tmp_path, monkeypatch, {
        "since": _ago(hours=2), "alerted": True, "last_alert": _ago(hours=1)})
    patch_usage(monkeypatch, util=0.2)
    main.run_pass(c, d)
    assert not dark_marker(c).exists()
    assert "auth_dark" not in d.notifier.sent
    patch_usage_dark(monkeypatch)
    main.run_pass(c, d)
    st = _marker(c)
    assert st["alerted"] is False and "last_alert" not in st
    assert "auth_dark" not in d.notifier.sent


def test_auth_dark_legacy_marker_realerts_once_past_interval(
        tmp_path, monkeypatch):
    since = _ago(hours=9)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    main.run_pass(c, d)
    st = _marker(c)
    assert d.notifier.sent.count("auth_dark") == 1
    assert st["since"] == since
    age = datetime.now(timezone.utc) - datetime.fromisoformat(st["last_alert"])
    assert abs(age.total_seconds()) < 10


def test_auth_dark_legacy_marker_realerts_exactly_at_interval(
        tmp_path, monkeypatch):
    since = (datetime.now(timezone.utc) - _realert_h()).isoformat()
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1


def test_auth_dark_legacy_marker_inside_interval_stays_silent(
        tmp_path, monkeypatch):
    since = _ago(hours=2)
    c, d = _dark_pass(tmp_path, monkeypatch, {"since": since, "alerted": True})
    main.run_pass(c, d)
    assert "auth_dark" not in d.notifier.sent
    assert _marker(c)["since"] == since


@pytest.mark.parametrize("raw", [
    "not valid json{{",
    "null",
    json.dumps({"alerted": True}),
    json.dumps({"since": "garbage", "alerted": True,
                "last_alert": _ago(hours=9)}),
    json.dumps({"since": _ago(hours=9), "alerted": True,
                "last_alert": "garbage"}),
], ids=["non-json", "null", "no-since", "bad-since", "bad-last-alert"])
def test_auth_dark_corrupt_marker_resets_without_alert(
        tmp_path, monkeypatch, raw):
    c, d = _dark_pass(tmp_path, monkeypatch, raw)
    main.run_pass(c, d)                        # must not raise
    st = _marker(c)
    assert st["alerted"] is False and "last_alert" not in st
    assert datetime.fromisoformat(st["since"]) > (
        datetime.now(timezone.utc) - timedelta(minutes=1))
    assert "auth_dark" not in d.notifier.sent


def _fail_then_recover(tmp_path, monkeypatch, marker):
    c, d = _dark_pass(tmp_path, monkeypatch, marker)
    before = _marker(c)
    d.notifier.msg_id = 0
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 1
    assert _marker(c) == before
    d.notifier.msg_id = 77
    main.run_pass(c, d)
    assert d.notifier.sent.count("auth_dark") == 2
    assert _marker(c)["last_alert"] != before.get("last_alert")


def test_failed_reminder_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(hours=9), "alerted": True,
        "last_alert": _ago(seconds=_realert_h(minutes=1).total_seconds())})


def test_failed_first_alert_is_retried_on_the_next_pass(tmp_path, monkeypatch):
    _fail_then_recover(tmp_path, monkeypatch, {
        "since": _ago(minutes=upkeep.AUTH_DARK_GRACE_MINUTES + 1),
        "alerted": False})

def test_pass_writes_heartbeat(tmp_path, monkeypatch):
    patch_usage(monkeypatch)
    c = cfg(tmp_path)
    d = deps()
    main.run_pass(c, d)
    hb = json.loads((Path(c.state_dir) / "pass.json").read_text())
    assert hb["interval_minutes"] == 5
    assert hb["started_at"] <= hb["finished_at"]


def test_reconcile_keeps_the_marker_of_a_task_that_is_still_starving(tmp_path):
    c = dc_replace(cfg(tmp_path), capacity=1)
    make_task(c, issue=41, slot=0, status=RUNNING)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    make_task(c, issue=43, slot=NO_SLOT, stage=Stage.PR_OPEN, status=waking(PR, "attention(feedback)"))
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).touch()
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 43).touch()
    upkeep._reconcile_wake_blocked(c)
    assert state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).exists()   # wake still denied
    assert state._wake_blocked_path(c.state_dir, "portfolio_eval", 43).exists()   # feedback spawn still denied


def test_reconcile_drops_the_marker_of_a_parked_task_with_pending_feedback(tmp_path):
    """A parked task is not spawned for its feedback (a wake starts the
    round): it waits for nothing."""
    c = cfg(tmp_path)
    make_task(c, issue=43, slot=NO_SLOT, stage=Stage.PR_OPEN, status=human())
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 43).parent.mkdir(parents=True, exist_ok=True)
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 43).touch()
    upkeep._reconcile_wake_blocked(c)
    assert not state._wake_blocked_path(c.state_dir, "portfolio_eval", 43).exists()


def test_reconcile_drops_a_marker_left_by_a_vanished_task(tmp_path):
    c = cfg(tmp_path)
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).parent.mkdir(parents=True, exist_ok=True)
    state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).touch()      # state file already flushed
    upkeep._reconcile_wake_blocked(c)
    assert not state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).exists()


def test_legacy_wake_blocked_marker_is_rekeyed_with_one_repository(tmp_path):
    c = cfg(tmp_path)
    make_task(c, issue=42, slot=NO_SLOT, status=waking())
    legacy = Path(c.state_dir) / "wake-blocked-42"
    legacy.touch()
    upkeep._migrate_legacy_keys(c)
    assert not legacy.exists()
    assert state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).exists()


def test_legacy_wake_blocked_marker_is_dropped_with_several_repositories(tmp_path):
    c = cfg(tmp_path)
    c = dc_replace(c, repositories=[c.repositories[0],
                                    dc_replace(c.repositories[0], name="factorial")])
    legacy = Path(c.state_dir) / "wake-blocked-42"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.touch()
    upkeep._migrate_legacy_keys(c)
    assert not legacy.exists()
    assert not state._wake_blocked_path(c.state_dir, "portfolio_eval", 42).exists()


# --- CLI wiring: --migrate-tmux (delete with dispatcher/tmux_migration.py) ---
