"""Driving one task a pass: observe its session, classify into one event and
apply it; launch-due wakes and their room (capacity, slot); the flush of
finished tasks and the access wake."""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dispatcher import execution_overrides, github_app, messages, relogin
from dispatcher.artifacts import (REVIEW_PAGE, TICKETS_DIR, check_spec, check_tickets,
                                  count_open_questions, page_revision, plan_revision,
                                  stamp_console_link)
from dispatcher.claims import box_free
from dispatcher.classify import (BackgroundView, Observation, Reads, artifact_path,
                                 classify, config_note)
from dispatcher.config import Config, Repository, policy_for
from dispatcher.deps import Admit, Deps
from dispatcher.executor import _Turn, _apply, _look
from dispatcher.launch_choice import _choose_launch, _execution_override_for
from dispatcher.lifecycle import Event, EventType, Stranded, Woken
from dispatcher.models import ModelPolicy, Order, track_gone
from dispatcher.stage_launch import _grace_elapsed, _park_for_login, _signal_of, _url
from dispatcher.state import (UNREADABLE_TASK_FILE, Stage, StageSignal, TaskState,
                              _clear_wake_blocked, _readable_tasks, _wake_blocked_path,
                              allocate_slot, has_waiting, load_all, max_slots,
                              next_stage, read_background, save)
from dispatcher.status import Kind, ended


def _answers_page(task: TaskState, artifact: str) -> tuple[str, str] | None:
    """The answers page a park names, worktree-relative, with its revision on
    disk; None without a page, or for one outside the worktree (an unusable
    reference)."""
    if not artifact:
        return None
    p = Path(artifact)
    resolved = p if p.is_absolute() else Path(task.worktree) / p
    try:
        wt_rel = str(resolved.resolve().relative_to(Path(task.worktree).resolve()))
    except ValueError:
        return None
    fingerprint, problem = page_revision(str(resolved))
    if problem:
        print(f"[warn] #{task.issue} answers page {wt_rel}: {problem}",
              file=sys.stderr)
    return wt_rel, fingerprint


def _expired(task: TaskState, cutoff_seconds: int) -> bool:
    """Finished for at least the retention window. An unparseable or
    missing terminal_at never expires: fail closed, the card stays."""
    if not ended(task.status):
        return False
    try:
        since = datetime.fromisoformat(task.terminal_at)
    except (TypeError, ValueError):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - since).total_seconds() >= cutoff_seconds


def _flush_finished(cfg: Config, deps: Deps, dry_run: bool = False) -> None:
    """Each finished task (done, failed, won't do) past the retention window
    hears `flush-due`; the table retires it."""
    by_name = {t.name: t for t in cfg.repositories}
    for task in load_all(cfg.state_dir):
        if not _expired(task, cfg.done_retention_days * 86400):
            continue
        turn = _Turn(cfg, deps, by_name.get(task.repository), admit=None, order=None,
                     dry_run=dry_run)
        _apply(turn, task, Event(EventType.FLUSH_DUE))


def _oldest_first(cfg: Config):
    """Sort key for box-wide queues: oldest `updated_at` first, ties in
    repository-list order so a pass is deterministic."""
    rank = {t.name: i for i, t in enumerate(cfg.repositories)}
    return lambda t: (t.updated_at, rank[t.repository])


def _wake_reached(cfg: Config, deps: Deps) -> None:
    """Each task parked for access whose repository the App reaches now hears
    `access-reached`; the table wakes it."""
    repositories = {r.name: r for r in cfg.repositories}
    for task in load_all(cfg.state_dir):
        if task.status.kind is not Kind.PARKED_ACCESS or task.repository not in repositories:
            continue
        repository = repositories[task.repository]
        try:
            deps.access.reached([repository.repo])
        except (github_app.AppError, github_app.NoAccess):
            continue   # asked again next pass
        turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=False)
        _apply(turn, task, Event(EventType.ACCESS_REACHED))


def _admission_candidates(cfg: Config, wants: Callable[[TaskState], bool]
                          ) -> list[tuple[Repository, TaskState]]:
    """(repository, task) for each task `wants` picks in a configured
    repository, oldest first."""
    repositories = {t.name: t for t in cfg.repositories}
    tasks = sorted(
        [t for t in load_all(cfg.state_dir)
         if t.repository in repositories and wants(t)],
        key=_oldest_first(cfg),
    )
    return [(repositories[t.repository], t) for t in tasks]


def _woken(task: TaskState) -> bool:
    """Waiting to launch, which the table decides (resume-admitted)."""
    return task.status.kind is Kind.WAKING


def _backfill_track(cfg: Config, policy: ModelPolicy, task: TaskState,
                    signal: StageSignal | None,
                    dry_run: bool) -> tuple[TaskState, StageSignal | None]:
    """Claimed before tracks existed (#121): no triage label was read and
    its spec signal names none. Run it as untracked work, the same as a
    candidate without a track label, and record that for the console."""
    task = replace(task, track=policy.untracked)
    if not dry_run:
        save(cfg.state_dir, task)
    if signal is not None and not signal.track:
        signal = replace(signal, track=task.track)
    return task, signal


def _adopt_track(cfg: Config, policy: ModelPolicy, task: TaskState,
                 signal: StageSignal | None,
                 dry_run: bool) -> tuple[TaskState, StageSignal | None]:
    """The task's track for this turn, settled before anything reads it."""
    if not task.track:
        task, signal = _backfill_track(cfg, policy, task, signal, dry_run)
    # Two signals set the track for every later stage (see StageSignal.track):
    # the spec session's `done` and the approval, a `done` at the plan gate.
    # Adopt it before anything below reads task.track —
    # the "track configured" guard and the launch this same turn may spawn
    # (e.g. PLAN off a spec "done" signal) both need the fresh value, not
    # whatever was recorded when the task was last saved. Only a CONFIGURED
    # track is adopted here: an unknown/misspelled one must reach
    # next_actions' bounce-then-park ladder (_bad_track) instead of being
    # written onto the task and mis-parked as "no longer configured".
    if (_sets_track(task, signal) and signal.track != task.track
            and signal.track in policy.tracks):
        task = replace(task, track=signal.track)
    return task, signal


def _sets_track(task: TaskState, signal: StageSignal | None) -> bool:
    """Is `signal` one that sets the track: the spec session's `done`, or the
    approval (a plan session's `done` once the operator approved)?"""
    return (signal is not None and signal.status == "done"
            and (task.stage is Stage.SPEC
                 or (task.stage is Stage.PLAN and bool(task.plan_approval))))


def _background_view(cfg: Config, deps: Deps, task: TaskState,
                     alive: bool) -> BackgroundView | None:
    """The task's background wait as this pass sees it; herdr is asked only
    when a live session has a background marker."""
    wait = read_background(cfg.state_dir, task.repository, task.issue) if alive else None
    if wait is None:
        return None
    return BackgroundView(wait, deps.sessions.agent_state(task.repository, task.issue),
                          time.time(), cfg.background_wait_seconds)


def _park_stranded(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                   why: str) -> None:
    """Park a waiting task the config strands for `why` (models.stranded):
    the table's track-unconfigured. A live session at a login prompt is
    held there instead (_park_for_login); that hold stays outside the table
    until the scheduler events move (ticket 09)."""
    note = config_note(why)
    tail = deps.sessions.capture_tail(task.repository, task.issue)
    login = relogin.classify_login(tail)
    if login is not None and _park_for_login(cfg, deps, repository, task, note, tail, login):
        return
    _apply(_Turn(cfg, deps, repository, None, None, False, tail=tail), task,
           Event(EventType.TRACK_UNCONFIGURED, Stranded(note, login="")))


def _driven(task: TaskState) -> bool:
    """A task whose session a pass observes; its wakes are _drive_wakes'."""
    return task.status.kind in (Kind.RUNNING, Kind.HELD_PLAN_APPROVAL)


def _drive_task(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                admit: Admit, order: Order, dry_run: bool = False) -> None:
    """One driven task's turn: observe, classify into one event, decide,
    execute."""
    turn = _Turn(cfg, deps, repository, admit, order, dry_run)
    task, event = _observed(turn, task)
    if event is None:
        return
    if task.hold_for_attach and _launch_admitted(event):
        _attach(turn, task, event)
    else:
        _apply(turn, task, event)


def _launch_admitted(event: Event) -> bool:
    return event.type is EventType.RESUME_ADMITTED and not event.detail.blocked


ATTACH_TEXT = ("The operator is attaching to talk to you directly. "
               "Wait for their input.")


def _attach(turn: _Turn, task: TaskState, event: Event) -> None:
    """An attach wake tells the session to wait for the operator: the notice
    is queued, so it rides with the operator's messages in whatever the
    session reads first, and it is taken back, and the operator is not
    pinged, when nothing launched."""
    notice = messages.append(turn.cfg.state_dir, task.repository, task.issue,
                             ATTACH_TEXT, "dispatcher")
    _apply(turn, task, event)
    if not turn.launched:
        messages.mark_delivered(turn.cfg.state_dir, task.repository, task.issue, [notice.id])
        return
    turn.deps.notifier.send("resumed_for_attach", issue=task.issue, title=task.title,
                            url=_url(turn.repository, task.issue),
                            repository=turn.repository.name,
                            note="new conversation" if turn.restarted else "")


def _observed(turn: _Turn, task: TaskState) -> tuple[TaskState, Event | None]:
    signal = _signal_of(task)
    policy = policy_for(turn.cfg, turn.repository)
    task, turn.signal = _adopt_track(turn.cfg, policy, task, signal, turn.dry_run)
    if task.status.kind is Kind.WAKING:
        return task, _launch_due(turn, task, policy)
    return task, classify(_observation(turn, task, policy))


def _launch_due(turn: _Turn, task: TaskState, policy: ModelPolicy) -> Event | None:
    """A waking task: resume-admitted once its next launch (state.next_stage)
    is, nothing while it is denied, parked when the config strands it."""
    why = track_gone(policy, task.track)
    stage = _wake_stage(task)
    launch = None
    if not why:
        launch, _bypass, why = _choose_launch(turn.cfg, turn.repository, task, stage,
                                              turn.admit, turn.order,
                                              _wake_override(turn, task, stage))
    if why:
        return Event(EventType.TRACK_UNCONFIGURED, Stranded(config_note(why), login=""))
    if launch is None:
        return None
    turn.launches[stage] = launch
    return _wake_room(turn, task)


def _wake_stage(task: TaskState) -> Stage:
    """The stage a wake launches: its next one, spec for a claim held back
    before its first launch."""
    stage = Stage(next_stage(task))
    return Stage.SPEC if stage is Stage.QUEUED else stage


def _wake_override(turn: _Turn, task: TaskState,
                   stage: Stage) -> execution_overrides.ExecutionOverride | None:
    """The model the resume named, else the stored execution override: "the
    next launch runs on this model" holds for a wake too."""
    return execution_overrides.pending(_execution_override_for(turn.cfg, task, stage),
                                       task.resume_model_override, task.resume_bypass_usage)


def _wake_room(turn: _Turn, task: TaskState) -> Event | None:
    """An admitted wake with no capacity or slot stays waking and says so
    once (WakeBlocked): no event while its marker stands."""
    blocked = _no_room(turn.cfg, task)
    if not blocked:
        _clear_wake_blocked(turn.cfg.state_dir, task.repository, task.issue)
    elif _wake_blocked_path(turn.cfg.state_dir, task.repository, task.issue).exists():
        return None
    return Event(EventType.RESUME_ADMITTED, Woken(blocked=blocked))


def _no_room(cfg: Config, task: TaskState) -> str:
    """Why the wake has no room; its own file is left out of the capacity
    count (a waking task holds no slot, so the slot count may keep it)."""
    all_tasks = _readable_tasks(cfg.state_dir)
    if all_tasks is None:
        return UNREADABLE_TASK_FILE
    others = [t for t in all_tasks
              if (t.repository, t.issue) != (task.repository, task.issue)]
    if box_free(cfg.capacity, others, triage_running=False) <= 0:
        return "capacity full"
    if allocate_slot(all_tasks, max_slots(cfg.capacity)) is None:
        return "no free slot"
    return ""


def _observation(turn: _Turn, task: TaskState, policy: ModelPolicy) -> Observation:
    cfg, sessions = turn.cfg, turn.deps.sessions
    alive = sessions.is_alive(task.repository, task.issue)
    # herdr's view of a background wait BEFORE the idle reading: a session
    # waking between the two reads must look still-waiting (held), never
    # woken-with-a-stale-idle (stall-parked mid-turn).
    view = _background_view(cfg, turn.deps, task, alive)
    # Idle only when it can matter: detection on and the session alive.
    stall_after = cfg.stall_after_seconds if alive else 0
    idle = sessions.idle_seconds(task.repository, task.issue) if stall_after > 0 else None
    return Observation(
        task=task, signal=turn.signal, alive=alive,
        waiting=has_waiting(cfg.state_dir, task.repository, task.issue),
        idle=idle, stall_after=stall_after, background=view, now=time.time(),
        poke_cap=cfg.background_wait_seconds, gone=track_gone(policy, task.track),
        grace_elapsed=_grace_elapsed(cfg, task), caps=cfg.loop_caps,
        tracks=frozenset(policy.tracks),
        gate_free=frozenset(n for n, t in policy.tracks.items() if not t.plan_review),
        reads=_reads(turn, task), look=_look(turn, task))


def _reads(turn: _Turn, task: TaskState) -> Reads:
    """What the worktree says about the report: a ready plan's tickets,
    revision and page; an approval's tickets; a spec; an answers page. A
    request about to be armed gets the console link stamped into its page
    first, so its revision covers the bytes on disk."""
    signal = turn.signal
    reader = _READS.get(signal.status) if signal is not None else None
    if reader is None:
        return Reads()
    _stamp_armed(turn, task, signal)
    return reader(task, signal)


def _stamp_armed(turn: _Turn, task: TaskState, signal: StageSignal) -> None:
    """Stamp the console link into the page of a request about to be armed."""
    # An answers park without a page arms no request: nothing to stamp.
    armed = signal.status == "awaiting-review" or (
        signal.status == "awaiting-answers" and signal.artifact)
    if armed and not turn.dry_run:
        stamp_console_link(task.worktree, signal.artifact, turn.cfg.console_url,
                           task.repository, task.issue)


def _ready_reads(task: TaskState, signal: StageSignal) -> Reads:
    return Reads(tickets=check_tickets(Path(task.worktree) / TICKETS_DIR),
                 revision=plan_revision(task.worktree, signal.artifact, task.spec_path),
                 page_questions=_page_questions(task, signal))


def _page_questions(task: TaskState, signal: StageSignal) -> int | None:
    """The question blocks on the prescribed review page, when the report
    names that page: it is what the gate would show."""
    page = Path(task.worktree) / REVIEW_PAGE
    try:
        if artifact_path(task, signal).resolve() != page.resolve():
            return None
        return count_open_questions(page)
    except (OSError, ValueError, RuntimeError):
        return None   # a path that cannot be resolved (a NUL character, a loop)


def _done_reads(task: TaskState, signal: StageSignal) -> Reads:
    if task.stage is Stage.SPEC:
        return Reads(spec=check_spec(artifact_path(task, signal)))
    tickets = check_tickets(Path(task.worktree) / TICKETS_DIR)
    if task.stage is not Stage.PLAN:
        return Reads(tickets=tickets)
    # The approval: the revision the dispatcher recorded when it drained the
    # operator's approve (never the worktree's answers file, which the
    # session writes), and the plan on disk now. The prescribed page, since
    # the request that named it is gone by now.
    return Reads(tickets=tickets,
                 revision=plan_revision(task.worktree, REVIEW_PAGE, task.spec_path),
                 approval=task.plan_approval)


def _answers_reads(task: TaskState, signal: StageSignal) -> Reads:
    return Reads(answers=_answers_page(task, signal.artifact))


_READS = {"awaiting-review": _ready_reads, "done": _done_reads,
          "awaiting-answers": _answers_reads}
