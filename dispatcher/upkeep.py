"""Per-pass upkeep around the real work: the budget-stall and auth-dark
alert edges, the pass heartbeat, snapshot pruning, legacy-key migration,
wake-blocked marker reconciliation and task-artifact sync."""
from __future__ import annotations

import json
import logging
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dispatcher import host_auth, messages, task_artifacts
from dispatcher.config import Config
from dispatcher.deps import Deps, _now
from dispatcher.operator_chat import _in_flight
from dispatcher.state import (WAKE_BLOCKED_PREFIX, TaskState, _clear_wake_blocked,
                              _wake_blocked_path, load_all, write_json_atomic)
from dispatcher.status import Kind, ended
from dispatcher.usage import ProviderUsage, Verdict, verdict_note

log = logging.getLogger(__name__)

# The weekly allowance grows continuously, so a box running at pace crosses
# zero headroom back and forth; the resume ping waits for this much to return.
RESUME_HEADROOM = 0.02


def _budget_edge(cfg: Config, deps: Deps, verdict: Verdict, now: datetime) -> None:
    """Edge-triggered stall/resume pings via a marker file, keyed on the
    verdict for the global policy default — what an idle box spawns next.
    Stall on the first denied pass; resume only once the default is admitted
    with at least RESUME_HEADROOM, or every zero crossing pings a pair.

    An unavailable provider also denies, but it is not a budget stall: there
    is no window and nothing will reset, so the "resumes when headroom
    returns" ping would send an operator off to wait out an outage that only
    a re-login ends — 30 minutes before the accurate auth_dark alert.
    _auth_dark_edge owns that case; stay silent for it."""
    if verdict.reason == "unavailable":
        return
    marker = Path(cfg.state_dir) / "budget-stalled"
    marker.parent.mkdir(parents=True, exist_ok=True)
    note = verdict_note(verdict, now)
    if not verdict.admitted and not marker.exists():
        marker.write_text(_now())
        deps.notifier.send("budget_stall", issue=0, title="(all tasks)", url="", note=note)
    elif _resumable(verdict) and marker.exists():
        marker.unlink()
        deps.notifier.send("budget_resume", issue=0, title="(all tasks)", url="", note=note)


def _resumable(verdict: Verdict) -> bool:
    """Admitted with at least RESUME_HEADROOM left, or with no binding window."""
    return verdict.admitted and (verdict.binding is None
                                 or verdict.binding.headroom >= RESUME_HEADROOM)


AUTH_DARK_GRACE_MINUTES = 30
AUTH_DARK_REALERT_HOURS = 8


def _auth_dark_edge(cfg: Config, deps: Deps, usages: dict[str, ProviderUsage]) -> None:
    """One alert per unknowable-usage incident, repeated every
    AUTH_DARK_REALERT_HOURS while it lasts (auth likely dead). Dark means
    EVERY fetched provider is unavailable; a single dark provider only fails
    its own models closed and stays visible on the console.

    Companion to _budget_edge: budget_stall covers "gate closed, headroom
    returns on its own"; this covers "we cannot even tell", which fail-safes
    every spawn indefinitely and therefore needs a human. The 30-minute grace
    absorbs transient API blips."""
    marker = Path(cfg.state_dir) / "auth-dark"
    if not _all_dark(usages):
        marker.unlink(missing_ok=True)
        return
    if not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"since": _now(), "alerted": False}))
        return
    now = datetime.now(timezone.utc)
    try:
        st = json.loads(marker.read_text())
        age_min, due = _dark_due(st, now)
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
        marker.write_text(json.dumps({"since": _now(), "alerted": False}))
        return
    # send returns 0 on failure: keep the marker so the next pass retries
    if due and deps.notifier.send("auth_dark", minutes=int(age_min),
                                  host=socket.gethostname()):
        marker.write_text(json.dumps({"since": st["since"], "alerted": True,
                                      "last_alert": _now()}))


def _all_dark(usages: dict[str, ProviderUsage]) -> bool:
    """No usage fetched, or every provider unavailable."""
    return not usages or all(u.source == "unavailable" for u in usages.values())


def _dark_due(st: dict, now: datetime) -> tuple[float, bool]:
    """Minutes since the incident began, and whether an alert is due now.
    Raises on a malformed marker."""
    since = datetime.fromisoformat(st["since"])
    alerted = bool(st.get("alerted"))
    # legacy marker (alerted, no last_alert): count the interval from since
    last = datetime.fromisoformat(st["last_alert"]) if alerted and "last_alert" in st else since
    age_min = (now - since).total_seconds() / 60
    # subtracting here also rejects offset-free timestamps (TypeError)
    due = (now - last >= timedelta(hours=AUTH_DARK_REALERT_HOURS) if alerted
           else age_min >= AUTH_DARK_GRACE_MINUTES)
    return age_min, due


SNAPSHOT_TTL_SECONDS = 7 * 24 * 3600


def _write_heartbeat(cfg: Config, started_at: str) -> None:
    """Atomic pass timestamp for the console's next-pass countdown. Written
    only when a pass completes: a crash-looping dispatcher goes stale, which
    the console surfaces as 'dispatcher not running?'. Never raises into a
    pass (same contract as eventlog.append_event)."""
    try:
        p = Path(cfg.state_dir) / "pass.json"
        write_json_atomic(p, {
            "started_at": started_at, "finished_at": _now(),
            "interval_minutes": cfg.pass_interval_minutes})
    except OSError as exc:
        print(f"[warn] heartbeat write failed: {exc}", file=sys.stderr)


def _prune_snapshots(cfg: Config) -> None:
    """A snapshot outlives its task by at most a week. In-flight tasks keep
    theirs regardless of age — the console may need it while they are
    parked. Every pass checks; ~200 KB per file makes eagerness cheap.

    The live set is built from state, not parsed from the stem: a
    "task-<repository>-<issue>.txt" stem can't be split back into (repository,
    issue) unambiguously (a repository name may itself contain digits and
    hyphens), so identity comes from load_all() same as everywhere else,
    same as _task_for_intent. The legacy "task-<issue>.txt" form is kept
    alive too, for any snapshot still sitting under its pre-rename name."""
    root = Path(cfg.state_dir) / "snapshots"
    if not root.exists():
        return
    live = _live_snapshot_names(cfg)
    if live is None:
        return  # unreadable/corrupt state dir: skip, never abort the sweep
    now = time.time()
    for p in root.glob("task-*.txt"):
        if p.name not in live:
            _unlink_if_expired(p, now)


def _live_snapshot_names(cfg: Config) -> set[str] | None:
    """The snapshot names of in-flight tasks, under both the current and
    the legacy form; None when the state dir cannot be read."""
    try:
        in_flight = [t for t in load_all(cfg.state_dir) if _in_flight(t)]
    except Exception:
        return None
    return ({f"task-{t.repository}-{t.issue}.txt" for t in in_flight}
            | {f"task-{t.issue}.txt" for t in in_flight})


def _unlink_if_expired(p: Path, now: float) -> None:
    """Delete a snapshot older than the TTL; one that vanished or cannot be
    read is left alone."""
    try:
        if now - p.stat().st_mtime > SNAPSHOT_TTL_SECONDS:
            p.unlink()
    except OSError:
        pass

def _starving(task: TaskState) -> bool:
    """Is this task legitimately still waiting for the capacity/slot it was
    denied? Exactly the queues that mark the marker, which wake it: a
    waking task (the wakes the table blocks, _wake_room). Anything else is not waiting for
    anything."""
    return task.status.kind is Kind.WAKING


def _migrate_legacy_keys(cfg: Config) -> None:
    """Take over pre-multi-project issue-only keys. Messages: see
    messages.migrate_legacy. Wake-blocked markers are transient (re-marked
    next pass if still true), so with several repositories a legacy one is just
    dropped; with one it is renamed to keep the edge-triggered event quiet."""
    names = [t.name for t in cfg.repositories]
    messages.migrate_legacy(cfg.state_dir, names)
    for p in Path(cfg.state_dir).glob(f"{WAKE_BLOCKED_PREFIX}*"):
        issue = p.name.removeprefix(WAKE_BLOCKED_PREFIX)
        if not issue.isdigit():
            continue
        if len(names) == 1:
            p.replace(_wake_blocked_path(cfg.state_dir, names[0], int(issue)))
        else:
            p.unlink(missing_ok=True)


def _reconcile_wake_blocked(cfg: Config) -> None:
    """Re-establish from disk, at the top of every pass, that the wake-blocked
    marker (and the "waiting for a free slot" badge and delivery_contract
    suffix it drives) stands only for a task that is _starving(): no
    terminal path has to remember it, and a re-claim of the same issue never
    inherits a stale badge. A marker whose task file is gone goes too. (A
    slot needs no such pass: the save gives it back with the status.)"""
    known: set[str] = set()
    for task in load_all(cfg.state_dir):
        known.add(_wake_blocked_path(cfg.state_dir, task.repository, task.issue).name)
        if not _starving(task):
            _clear_wake_blocked(cfg.state_dir, task.repository, task.issue)
    for p in Path(cfg.state_dir).glob(f"{WAKE_BLOCKED_PREFIX}*"):
        if p.name not in known:  # state file flushed/deleted under the marker
            p.unlink(missing_ok=True)



def _sync_artifacts(cfg: Config, *, dry_run: bool = False,
                    auth: host_auth.HostAuth = host_auth.STORED) -> None:
    repos = {repository.name: repository.repo for repository in cfg.repositories}
    for task in load_all(cfg.state_dir):
        try:
            task_artifacts.collect(cfg.state_dir, task, repos.get(task.repository, ""),
                                   publish=not dry_run and not ended(task.status),
                                   auth=auth)
        except (OSError, ValueError, host_auth.HostWait):
            log.exception("Artifact collection failed for %s/%s", task.repository, task.issue)
    task_artifacts.cleanup(cfg.state_dir)
