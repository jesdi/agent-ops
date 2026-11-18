"""The executor: a _Turn of decided effects applied to a task, the _ACT
handler for each effect, admissions, notices and crash reports."""
from __future__ import annotations

import logging
import re
import shlex
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from dispatcher import (answers, eventlog, execution_overrides, failures, loops, relogin,
                        spec_publish, task_artifacts)
from dispatcher.board import BoardError
from dispatcher.classify import Look, config_note
from dispatcher.config import Config, Repository
from dispatcher.deps import Admit, Deps, _now
from dispatcher.launch_choice import (Launch, _choose_launch, _consume_execution_choice,
                                      _drop_override, _last_launched, _saved_override)
from dispatcher.lifecycle import (AcquireSlot, Admission, ApplyRound, At, CancelIssue,
                                  ClearTurnMarkers, ContinueSession, EndPokeStretch,
                                  EndSession, EnterGate, Event, EventType, FinishMerged, Flush,
                                  Ignore, LaunchFailure, LinkPR, MarkAsked, Notify, Pane, Poke,
                                  PublishSpec, RecordBackgroundWait, Reject, ReleaseCard,
                                  ReleaseSlot, ReportCrash, ReportTaskCrash, ResetRounds,
                                  RespawnStage, RetryStage, SetResumeOverride,
                                  SpawnAddressReview, SweepTombstone, Transition, WakeBlocked,
                                  decide, login_at, operator_request)
from dispatcher.loops import Decision, Outcome, ResetCause
from dispatcher.models import Order, parse_entry, stage_pick
from dispatcher.runtimes import runtime_for
from dispatcher.stage_launch import (_address_review_round, _continue_woken, _continued,
                                     _end_session, _heal_exclude, _mark_wake_blocked, _notify,
                                     _resume_text, _resumed, _spawned, _spec_note,
                                     _stage_started, _task_token, _url, _with_tail,
                                     _write_launching, _write_working)
from dispatcher.state import (NO_POKES, NO_SLOT, Stage, StageSignal, TaskState,
                              _clear_wake_blocked, _rank_rejected_path, _readable_tasks,
                              allocate_slot, at_gate, clear_session, clear_turn_markers,
                              delete, end, load, max_slots, read_session, save)
from dispatcher.status import _WAKEABLE, FailReason, Kind, Status, occupies
from dispatcher.workspace import remove_workspace

log = logging.getLogger(__name__)


def _crash_failed(task: TaskState) -> TaskState:
    """Failed by a task crash at the stage it had, which Resume starts
    afresh (state.next_stage)."""
    return replace(end(task, Status(Kind.FAILED, reason=FailReason.TASK_CRASH)),
                   updated_at=_now())


def _fail_task_crash(cfg: Config, deps: Deps, repository: Repository,
                     task: TaskState, dry_run: bool = False) -> None:
    """One task's turn blew up. Fail THAT task; let the pass carry on.

    Everything a task's turn touches is reachable through its worktree, and
    a worktree can simply be gone — swept, half-removed, or deleted by hand.
    The launch then raises writing into the missing worktree, and before
    this the exception unwound all the way out of run_pass: guarded_pass
    filed a pass-crash and re-raised, so the unit failed and every task
    QUEUED BEHIND the broken one was never reached. One dead checkout stopped
    the whole box, every firing, until a human looked. A card in Failed is
    the far cheaper outcome, and the report carries the traceback.

    The failure is decided (task-crashed) on the saved state: the turn may
    have persisted a step before it failed, and the caller's `task` predates
    that write. From the caller's task when the state file cannot be read,
    and written directly when the table cannot read the task: this function
    must not raise."""
    turn = _Turn(cfg, deps, repository, None, None, dry_run, error=traceback.format_exc())
    try:
        task = load(cfg.state_dir, task.repository, task.issue) or task
    except Exception as exc:
        print(f"[warn] state of #{task.issue} unreadable after its crash: "
              f"{exc}", file=sys.stderr)
    try:
        _apply(turn, task, Event(EventType.TASK_CRASHED))
    except Exception as exc:
        print(f"[warn] #{task.issue} failed outside the table: {exc}", file=sys.stderr)
        save(cfg.state_dir, _crash_failed(task))


@dataclass
class _Turn:
    """One task's turn: what the executor and its action handlers read, and
    what an action leaves for after the commit."""
    cfg: Config
    deps: Deps
    repository: Repository | None       # None: the task's repository left the config
    admit: Admit | None
    order: Order | None
    dry_run: bool
    signal: StageSignal | None = None   # the stage signal read at the start of the turn
    error: str = ""                     # the traceback of a crashed turn
    tail: str | None = None             # the pane, read once before the session ends
    spec_line: str = ""                 # the spec-publish line a later notice appends
    launches: dict[Stage, Launch] = field(default_factory=dict)  # admitted, by stage
    gh_config: str = ""
    launched: bool = False              # a launch went through: the choice is used up
    restarted: str = ""                 # why a new conversation replaced the recorded one
    event: Event | None = None
    msg_id: int = 0                     # the park question's message
    actor: str = "dispatcher"           # who sent the event: the dispatcher or the operator
    after: list[Callable[[TaskState], None]] = field(default_factory=list)


class _LaunchFailed(Exception):
    """A launch raised: what the hand-over accepted, and the stage that
    could not start."""

    def __init__(self, stage: Stage, accepted: TaskState):
        super().__init__(stage.value)
        self.stage, self.accepted = stage, accepted


def _apply(turn: _Turn, task: TaskState, event: Event) -> Transition | Ignore | Reject:
    """Decide `event` for `task` and execute what the table answers: the
    actions that end or start a session, then one commit and its
    `transition` event, then the notices (ADR 0015). Returns the answer."""
    outcome = decide(task, event)
    if isinstance(outcome, Reject):
        _rejected(turn, task, event, outcome.reason)
    if not isinstance(outcome, Transition):
        log.debug("#%s %s: %s", task.issue, event.type.value, outcome.reason)
        return outcome
    turn.event = event
    if _launches(outcome) and not _token(turn, task):
        return outcome
    _execute(turn, task, outcome)
    if turn.launched:
        _consume_execution_choice(turn.cfg, task.repository, task.issue)
    return outcome


def _rejected(turn: _Turn, task: TaskState, event: Event, reason: str) -> None:
    """The table refused `event`: a `transition-rejected` event says why."""
    eventlog.append_event(turn.cfg.state_dir, "transition-rejected", repository=task.repository,
                          issue=task.issue, stage=task.stage.value, actor=turn.actor,
                          detail=reason,
                          transition={"from": task.status.kind.value,
                                      "event": event.type.value, "stage": task.stage.value})


def _execute(turn: _Turn, task: TaskState, t: Transition) -> None:
    before = At(task.stage, task.status)
    if turn.tail is None and any(isinstance(a, Notify) and a.template in _TAIL_NOTICES
                                 for a in t.actions):
        turn.tail = turn.deps.sessions.capture_tail(task.repository, task.issue)
    try:
        after = _act(turn, task, t)
    except _LaunchFailed as failed:
        turn.error, turn.after = traceback.format_exc(), []
        turn.event = Event(EventType.LAUNCH_FAILED, LaunchFailure(failed.stage))
        t = decide(task, turn.event)
        after = _act(turn, failed.accepted, t)
    _settle(turn, _commit(turn, before, after, t), t, before)


def _look(turn: _Turn, task: TaskState) -> Look:
    """What classify reads of the session only when its event needs it."""
    return Look(pane=lambda: Pane.UNKNOWN if turn.dry_run else _pane(turn.deps, task),
                login=lambda: _login_prompt(turn, task),
                launches=lambda stages: _stage_admissions(turn, task, stages))


def _pane(deps: Deps, task: TaskState) -> Pane:
    """One read of herdr and one of the pane. Text plus Enter in a prompt
    can approve an action without the operator, so a read that failed is
    never taken as "no prompt"."""
    state = deps.sessions.agent_state(task.repository, task.issue)
    tail = deps.sessions.capture_tail(task.repository, task.issue)
    if state is None or not tail.strip():
        return Pane.UNKNOWN
    if state[0] == "working":
        return Pane.WORKING
    if state[0] == "blocked" or relogin.classify_login(tail) is not None:
        return Pane.PROMPT
    return Pane.CLEAR


def _login_prompt(turn: _Turn, task: TaskState) -> str:
    """The login prompt in the pane of a session that holds a slot, where a
    park holds instead; the pane is kept for the notice."""
    if not occupies(task.status):
        return ""
    turn.tail = turn.deps.sessions.capture_tail(task.repository, task.issue)
    login = relogin.classify_login(turn.tail)
    if login is None:
        return ""
    return login.url or "(attach to the session to see the URL)"


def _stage_admissions(turn: _Turn, task: TaskState,
                      stages: tuple[Stage, ...]) -> tuple[Admission, ...]:
    """Each launch an event's rule may start, chosen as every launch is
    (_choose_launch), read before decide; the admitted ones are kept for the
    executor. A saved choice is dropped only when it may apply to none of
    them: it may be meant for the one the rule does not pick."""
    saved = {stage: _saved_override(turn.cfg, task, stage) for stage in stages}
    _drop_if_refused_by_all(turn.cfg, task, saved)
    return tuple(_admission(turn, task, stage, saved[stage][0]) for stage in stages)


def _drop_if_refused_by_all(cfg: Config, task: TaskState, saved: dict) -> None:
    refusals = [(stage, why) for stage, (_, why) in saved.items() if why]
    if saved and len(refusals) == len(saved):
        _drop_override(cfg, task, *refusals[0])


def _admission(turn: _Turn, task: TaskState, stage: Stage,
               override: execution_overrides.ExecutionOverride | None) -> Admission:
    launch, _bypass, why = _choose_launch(turn.cfg, turn.repository, task, stage,
                                          turn.admit, turn.order, override)
    if launch is not None:
        turn.launches[stage] = launch
    return Admission(stage, admitted=launch is not None,
                     stranded=config_note(why) if why else "")


LaunchAction = RespawnStage | RetryStage | ContinueSession | SpawnAddressReview


def _launch_action(t: Transition) -> LaunchAction | None:
    return next((a for a in t.actions if isinstance(a, LaunchAction)), None)


def _launches(t: Transition) -> bool:
    return _launch_action(t) is not None


def _token(turn: _Turn, task: TaskState) -> bool:
    """The launch's token before anything changes: an App error leaves the
    turn for the next pass; an unreached repository parks the task."""
    gh_config = _task_token(turn.cfg, turn.deps, turn.repository, task)
    turn.gh_config = gh_config or ""
    return gh_config is not None


def _act(turn: _Turn, task: TaskState, t: Transition) -> TaskState:
    for act in t.actions:
        task = _ACT[type(act)](turn, task, act)
    return task


def _commit(turn: _Turn, before: At, after: TaskState, t: Transition) -> TaskState:
    """The one save of a transition, and its `transition` event. updated_at
    moves only with the stage or the status: the grace clock and the wake
    order read it."""
    changed = (before.stage, before.status) != (t.stage, t.status)
    stored = replace(after, stage=t.stage, status=t.status, park_msg_id=0,
                     hold_for_attach=after.hold_for_attach and t.status.kind is Kind.WAKING,
                     plan_approval=_kept_approval(after, t),
                     updated_at=_now() if changed else after.updated_at,
                     **_parked_choice(t.status))
    _discard_stale_answers(before, t, stored)
    save(turn.cfg.state_dir, stored)
    eventlog.append_event(turn.cfg.state_dir, "transition", repository=stored.repository,
                          issue=stored.issue, stage=t.stage.value, actor=turn.actor,
                          transition={"from": before.status.kind.value,
                                      "event": turn.event.type.value,
                                      "to": t.status.kind.value, "stage": t.stage.value})
    return stored


# The statuses a recorded plan approval outlives at plan: the wake it sends
# and the session that reads it, and a park of that session for the operator.
_APPROVAL_KEPT = frozenset({Kind.RUNNING, Kind.WAKING, Kind.HELD_LOGIN, Kind.PARKED_HUMAN,
                            Kind.PARKED_ANSWERS})


def _kept_approval(task: TaskState, t: Transition) -> str:
    """The plan approval applies to the plan session it woke: a new review
    round (held or parked for review), the move off plan or an end stops
    it applying."""
    kept = t.stage is Stage.PLAN and t.status.kind in _APPROVAL_KEPT
    return task.plan_approval if kept else ""


def _parked_choice(status: Status) -> dict:
    """A park for the operator ends a wake: the wake that follows it says
    for itself on which model."""
    if status.kind in (Kind.PARKED_HUMAN, Kind.PARKED_ANSWERS):
        return {"resume_model_override": "", "resume_bypass_usage": False}
    return {}


def _discard_stale_answers(before: At, t: Transition, task: TaskState) -> None:
    """A newly armed request drops the answers of an earlier revision."""
    request = operator_request(At(t.stage, t.status))
    if request is None:
        return
    old = operator_request(before)
    if old is None or (old.kind, old.path, old.fingerprint) != (
            request.kind, request.path, request.fingerprint):
        answers.discard_stale(task.worktree, request.kind, request.fingerprint)


def _settle(turn: _Turn, saved: TaskState, t: Transition, before: At) -> None:
    """After the commit: the history events, then the notices in the order
    the actions named them; a park keeps the message that asked. A stay in
    the same status (a flush, a swept tombstone) is no new history."""
    if before.status != t.status:
        _legacy_event(turn, saved, t)
    for effect in turn.after:
        effect(saved)
    _remember_question(turn, saved)


def _remember_question(turn: _Turn, saved: TaskState) -> None:
    """The one write after the commit: the id of the message a park's notice
    got, which only the send returns, and the notice goes out only after the
    commit (ADR 0015). An operator's reply to it is matched by that id; a
    crash before this write leaves 0, which the console shows as a question
    not yet sent. A fact, never the stage or the status."""
    if turn.msg_id and saved.status.kind in _WAKEABLE:
        save(turn.cfg.state_dir, replace(saved, park_msg_id=turn.msg_id))


_REVIEW_PARKED = {EventType.GRACE_EXPIRED: "plan review grace expired",
                  EventType.SESSION_SIGNAL: "unattended review rounds used up",
                  EventType.SESSION_DIED: ("plan session died at the gate; "
                                           "unattended rounds used up")}
_FAILED = {FailReason.SESSION_CRASH: "session crashed mid-stage",
           FailReason.TASK_CRASH: "task crashed mid-pass"}
# The operator's ends: the status a kill or a cancel leaves when the table
# cannot take it (_end_untracked), and the history event the console reads.
_OPERATOR_ENDS = {EventType.OP_KILL: (Status(Kind.FAILED, reason=FailReason.KILLED),
                                      "killed by operator"),
                  EventType.OP_CANCEL: (Status(Kind.CANCELED), "canceled by operator")}


# The event the console's history reads for a park or a crash, by status.
_HISTORY: dict[Kind, Callable[[Event, Status], str]] = {
    Kind.PARKED_HUMAN: lambda e, s: s.note,
    Kind.PARKED_ANSWERS: lambda e, s: s.note,
    Kind.HELD_LOGIN: lambda e, s: "needs re-login: " + s.note,
    Kind.PARKED_CI: lambda e, s: f"awaiting CI run {s.run_id}",
    Kind.PARKED_REVIEW: lambda e, s: _REVIEW_PARKED[e.type],
}


def _history(event: Event, status: Status) -> tuple[str, str] | None:
    if status.kind in _HISTORY:
        return "parked", _HISTORY[status.kind](event, status)
    if status.kind is Kind.FAILED and status.reason in _FAILED:
        return "failed", _FAILED[status.reason]
    end = _OPERATOR_ENDS.get(event.type)
    return (end[0].kind.value, end[1]) if end else None


def _legacy_event(turn: _Turn, saved: TaskState, t: Transition) -> None:
    entry = _history(turn.event, t.status)
    if entry is not None:
        eventlog.append_event(turn.cfg.state_dir, entry[0], repository=saved.repository,
                              issue=saved.issue, stage=saved.stage.value, actor=turn.actor,
                              detail=entry[1])


# ---- action handlers: (turn, task, action) -> the task they leave ----------

def _acquire_slot(turn: _Turn, task: TaskState, act: AcquireSlot) -> TaskState:
    if task.slot != NO_SLOT:
        return task
    # The launch was checked for room (_no_room); this is the last guard.
    tasks = _readable_tasks(turn.cfg.state_dir)
    slot = None if tasks is None else allocate_slot(tasks, max_slots(turn.cfg.capacity))
    if slot is None:
        raise RuntimeError(f"#{task.issue}: no free slot for its launch")
    return replace(task, slot=slot)


def _release_slot(turn: _Turn, task: TaskState, act: ReleaseSlot) -> TaskState:
    return replace(task, slot=NO_SLOT)


def _ended(turn: _Turn, task: TaskState, act: EndSession) -> TaskState:
    _end_session(turn.cfg, turn.deps, task.repository, task.issue)
    return task


def _handover(turn: _Turn, task: TaskState, act: RespawnStage) -> tuple[TaskState, str]:
    """What the previous stage hands the launch: the spec session's `done`
    hands spec.md and the track to plan; a step from the gate is a round
    there; an implement start records its ticket set."""
    spec_path = ""
    if task.stage is Stage.SPEC and act.stage is Stage.PLAN:
        spec_path = turn.signal.artifact
        task = replace(task, track=turn.signal.track)
    elif at_gate(task):
        task = replace(task, **loops.gate_fields(task, respawn=act.stage is Stage.PLAN))
    if act.tickets:
        task = replace(task, ticket_count=act.tickets)
    return task, spec_path


def _respawned(turn: _Turn, task: TaskState, act: RespawnStage) -> TaskState:
    """The previous stage's session is usually still alive: an interactive
    session cannot exit itself, and the launch would type the next stage's
    command INTO it. End it first. stage.json says `working` before the
    launch (which refuses an .agent that is not a real directory), so the
    new session's own report is never overwritten and the old one is never
    read again; it keeps the hand-over, so a dispatcher killed before the
    commit repeats the launch."""
    task, spec_path = _handover(turn, task, act)
    task, message = _woken_respawn(turn, task)
    launch = turn.launches[act.stage]
    task = loops.reset(task, ResetCause.STAGE_STARTED)

    def spawn(task: TaskState) -> TaskState:
        _write_launching(task.worktree, act.stage, launch.entry)
        return _spawned(turn.cfg, turn.deps, turn.repository, task, launch, spec_path,
                        message, gh_config=turn.gh_config)
    task = _relaunched(turn, task, act.stage, spawn, accepted=replace(
        task, stage=act.stage, spec_path=spec_path or task.spec_path))
    turn.after.append(lambda saved: _working_launched(turn, saved, launch))
    return task


def _relaunched(turn: _Turn, task: TaskState, stage: Stage,
                start: Callable[[TaskState], TaskState],
                accepted: TaskState | None = None) -> TaskState:
    """End the task's pane, then `start` a session for `stage`: a launch
    into a live pane would type its command INTO that session's input box.
    A raising launch fails with `accepted` (the task as handed over)."""
    _end_session(turn.cfg, turn.deps, task.repository, task.issue)
    return _started(turn, task, stage, start, accepted)


def _started(turn: _Turn, task: TaskState, stage: Stage,
             start: Callable[[TaskState], TaskState],
             accepted: TaskState | None = None) -> TaskState:
    """`start` a session for `stage` where none runs; a raising launch fails
    with `accepted` (the task as handed over)."""
    try:
        task = start(task)
    except Exception as exc:
        raise _LaunchFailed(stage, accepted or task) from exc
    turn.launched = True
    return task


def _woken_respawn(turn: _Turn, task: TaskState) -> tuple[TaskState, str]:
    """A wake's respawn uses up its one-shot model and resume text; the text
    rides in the fresh stage prompt. A fresh prompt, not a resume: a crashed
    launch may never have started a conversation."""
    if task.status.kind is not Kind.WAKING:
        return task, ""
    message = _resume_text(task)
    origin = task.status.origin
    if origin is not None and origin.kind is Kind.FAILED:
        task = _crash_resumed(turn, task)
        message = f"This stage restarts after it failed ({origin.reason.value}).\n\n{message}"
    return replace(task, resume_model_override="", resume_bypass_usage=False,
                   resume_text=""), message.strip()


def _crash_resumed(turn: _Turn, task: TaskState) -> TaskState:
    """The resume of a failed task: its card is back in progress, and the
    history says it resumed after the failure."""
    turn.deps.github.set_status(turn.repository, task.issue, "In progress")
    turn.after.append(lambda saved: eventlog.append_event(
        turn.cfg.state_dir, "resumed", repository=saved.repository, issue=saved.issue,
        stage=saved.stage.value, model=str(turn.launches[saved.stage].entry),
        detail="after crash"))
    return replace(task, **loops.crash_resume_fields(task))


def _session_continued(turn: _Turn, task: TaskState, act: ContinueSession) -> TaskState:
    """An admitted wake continues its stage's recorded conversation. The
    pane is ended first: the resume command would otherwise be typed INTO
    a live session's input box."""
    launch, message = turn.launches[task.stage], _resume_text(task)

    def resume(task: TaskState) -> TaskState:
        _heal_exclude(turn.repository, task)
        task, turn.restarted = _continue_woken(turn.cfg, turn.deps, turn.repository, task,
                                               launch, message, turn.gh_config)
        return task
    return _relaunched(turn, task, task.stage, resume)


def _address_review_spawned(turn: _Turn, task: TaskState, act: SpawnAddressReview) -> TaskState:
    """An admitted wake at pr-open starts an address-review round. Its pane
    is ended first: a task parked at pr-open before that transition ended
    the session, or a legacy loop-cap park, may still hold a live one."""
    launch, message = turn.launches[Stage.ADDRESS_REVIEW], _resume_text(task)
    return _relaunched(turn, task, Stage.ADDRESS_REVIEW, lambda task: _address_review_round(
        turn.cfg, turn.deps, turn.repository, task, launch, message, turn.gh_config))


def _working_launched(turn: _Turn, saved: TaskState, launch: Launch) -> None:
    _stage_started(turn.cfg, saved, launch)


# What a resumed session is told to do about its rejected signal.
_RETRY_HINT = {
    Stage.SPEC: ("Re-write the signal to fix that (the track list, with each "
                 "track's meaning, is in your stage prompt)."),
    Stage.PLAN: ("Fix that in place, then re-write the signal (the signals, "
                 "and the track list with each track's meaning, are in your "
                 "stage prompt). The ticket set under .agent/tickets/ must be "
                 "files named NN-slug.md numbered contiguously from 01 with no "
                 "gaps or duplicates, each with a 'What to build' section, a "
                 "'Blocked by' line and at least one unchecked '- [ ]' "
                 "criterion. Do not re-plan from scratch."),
}


def _retried(turn: _Turn, task: TaskState, act: RetryStage) -> TaskState:
    """Resume the spec or plan session with why its signal was rejected, in
    place, rather than failing the task. The resume reads the transcript
    from the runtime's mounted home, so the (zombie) session is ended first,
    or the resume command would be typed INTO its input box."""
    _end_session(turn.cfg, turn.deps, task.repository, task.issue)
    text = f"Your .agent/stage.json was rejected: {act.reason}. {_RETRY_HINT[act.stage]}"
    launch = turn.launches[act.stage]
    try:
        _write_working(task.worktree, act.stage, launch.entry)
        task, detail = _continued(turn.cfg, turn.deps, turn.repository, task, launch,
                                  text, gh_config=turn.gh_config)
    except Exception as exc:
        raise _LaunchFailed(act.stage, task) from exc
    turn.launched = True
    turn.after.append(lambda saved: _working_resumed(turn, saved, launch, detail))
    return loops.spend_retry(task, act.retry)


def _working_resumed(turn: _Turn, saved: TaskState, launch: Launch, detail: str) -> None:
    if detail:
        _stage_started(turn.cfg, saved, launch)
    _resumed(turn.cfg, saved, launch, detail)


def _published(turn: _Turn, task: TaskState, act: PublishSpec) -> TaskState:
    """The spec folder committed and pushed before anyone is told; the link
    (or a local-only warning) rides in the next notice, and the issue's
    comment is posted after the commit, like every notice."""
    if not task.spec_path:
        return task
    pub = spec_publish.ensure_published(
        worktree=task.worktree, branch=task.branch,
        repo=turn.repository.repo, issue=task.issue,
        artifact=task.spec_path, dry_run=turn.dry_run, auth=turn.deps.github.auth)
    turn.spec_line = _spec_note(pub)
    if act.review and not pub.error:
        turn.after.append(lambda saved: _comment_spec(turn, saved, pub.url))
    return task


def _comment_spec(turn: _Turn, task: TaskState, url: str) -> None:
    try:
        turn.deps.github.comment(turn.repository, task.issue,
                                 f"📝 Plan ready for review: {url}")
    except Exception as exc:
        print(f"[warn] spec link comment failed for #{task.issue}: {exc}", file=sys.stderr)


def _counted(turn: _Turn, task: TaskState, act: ApplyRound) -> TaskState:
    turn.after.append(lambda saved: _round_told(turn, saved, act.decision))
    return act.decision.apply_to(task)


def _round_told(turn: _Turn, saved: TaskState, d: Decision) -> None:
    eventlog.append_event(turn.cfg.state_dir, "round", repository=saved.repository,
                          issue=saved.issue, stage=saved.stage.value, detail=d.description)
    if d.outcome is Outcome.LAST_ROUND:
        _notify(turn.deps, turn.repository, saved, "last_round",
                f"{d.loop.value} round {d.round}/{d.cap}")


def _poked(turn: _Turn, task: TaskState, act: Poke) -> TaskState:
    """The first attempt starts the cap clock, delivered or not: a pane that
    refuses text every pass is then still parked at the cap. Only a
    delivered poke is counted and logged."""
    task = replace(task, poked_since=task.poked_since or time.time())
    if not turn.deps.sessions.send_text(task.repository, task.issue, act.text):
        print(f"[warn] poke of #{task.issue} not delivered", file=sys.stderr)
        return task
    turn.after.append(lambda saved: eventlog.append_event(
        turn.cfg.state_dir, "poked", repository=saved.repository, issue=saved.issue,
        stage=saved.stage.value, detail=f"poke {saved.pokes}"))
    return replace(task, pokes=task.pokes + 1)


def _stretch_ended(turn: _Turn, task: TaskState, act: EndPokeStretch) -> TaskState:
    return replace(task, **NO_POKES)


def _wait_recorded(turn: _Turn, task: TaskState, act: RecordBackgroundWait) -> TaskState:
    return replace(task, background_reported=act.reported, background_seq=act.seq,
                   **NO_POKES)


def _crash_reported(turn: _Turn, task: TaskState, act: ReportCrash) -> TaskState:
    """The card goes back; the pane is read for the report before the
    session is ended, and the report is filed after the commit."""
    turn.deps.github.release(turn.repository, task.issue, "session crashed mid-stage")
    log_tail = turn.deps.sessions.capture_tail(task.repository, task.issue, lines=30)
    turn.after.append(lambda saved: _report_session_crash(
        turn.cfg, turn.deps, turn.repository, task, turn.dry_run, log_tail))
    return task


def _task_crash_reported(turn: _Turn, task: TaskState, act: ReportTaskCrash) -> TaskState:
    """Best-effort teardown: nothing here may raise, or the pass dies with
    the task."""
    try:
        turn.deps.github.release(turn.repository, task.issue, "task crashed mid-pass")
    except Exception as exc:
        print(f"[warn] release failed for #{task.issue}: {exc}", file=sys.stderr)
    try:
        log_tail = turn.deps.sessions.capture_tail(task.repository, task.issue, lines=30)
    except Exception:
        log_tail = ""
    turn.after.append(lambda saved: _report_task_crash(turn, task, log_tail))
    return task


def _report_task_crash(turn: _Turn, task: TaskState, log_tail: str) -> None:
    # klass is not "session-crash": nothing is wrong inside the container,
    # so this routes to infra_repo rather than the repository's repo.
    rep = failures.FailureReport(
        klass="task-crash", repository=turn.repository.name, issue=task.issue,
        title=f"task #{task.issue} crashed mid-pass: {task.title}",
        error=turn.error, log_tail=log_tail,
        repro=f"agent-ops-dispatcher  # with task-{task.issue} at stage {task.stage.value}",
        worktree=task.worktree)
    failures.report_failure(turn.cfg, turn.deps, rep, dry_run=turn.dry_run)


def _marked_asked(turn: _Turn, task: TaskState, act: MarkAsked) -> TaskState:
    return replace(task, asked=True)


def _gate_entered(turn: _Turn, task: TaskState, act: EnterGate) -> TaskState:
    return replace(task, **_gate_round_fields(task))


def _gate_round_fields(task: TaskState) -> dict:
    """The gate's round fields; a plan entering the gate starts the gate
    phase's own retries."""
    extra = dict(loops.gate_fields(task))
    if not at_gate(task):
        extra.update(plan_retries=0, plan_slips=0)
    return extra


def _pr_linked(turn: _Turn, task: TaskState, act: LinkPR) -> TaskState:
    """pr-open, with the PR number its signal links (kept when none): the
    artifact, else the note."""
    clear_turn_markers(turn.cfg.state_dir, task.repository, task.issue)
    text = (turn.signal.artifact or turn.signal.note or "") if turn.signal else ""
    m = re.search(r"/pull/(\d+)", text)
    turn.after.append(lambda saved: eventlog.append_event(
        turn.cfg.state_dir, "pr-opened", repository=saved.repository, issue=saved.issue,
        stage=Stage.PR_OPEN.value))
    return replace(task, pr_number=int(m.group(1)) if m else task.pr_number)


def _wake_blocked(turn: _Turn, task: TaskState, act: WakeBlocked) -> TaskState:
    _mark_wake_blocked(turn.cfg, turn.repository, task, act.reason)
    return task


def _markers_cleared(turn: _Turn, task: TaskState, act: ClearTurnMarkers) -> TaskState:
    clear_turn_markers(turn.cfg.state_dir, task.repository, task.issue)
    return replace(task, **NO_POKES)


def _noticed(turn: _Turn, task: TaskState, act: Notify) -> TaskState:
    turn.after.append(lambda saved: _send_notice(turn, saved, act))
    return task


def _question(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    return {"note": _with_tail(act.note, turn.tail or "")}


def _relogin(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    return {"note": _with_tail(act.note, turn.tail or ""), "login_url": login_at(turn.event)}


def _plan_parked(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    note = (turn.tail or "").strip() or "(no detail)"
    return {"note": note + (f"\n{turn.spec_line}" if turn.spec_line else ""),
            "rounds": saved.unattended_rounds}


def _plain(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    return {"note": act.note + (f"\n{turn.spec_line}" if turn.spec_line else "")}


def _merged_note(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    return {"note": f"https://github.com/{turn.repository.repo}/pull/{saved.pr_number}"}


def _closed_note(turn: _Turn, saved: TaskState, act: Notify) -> dict:
    return {"note": f"PR #{saved.pr_number}. Worktree preserved for autopsy."}


# How each notice is worded; the ones that carry the pane first.
_TAIL_NOTICES = {"parked_question": _question, "needs_relogin": _relogin,
                 "plan_parked": _plan_parked, "plan_session_stopped": _plan_parked}
_NOTICES = {**_TAIL_NOTICES, "task_done": _merged_note, "pr_closed": _closed_note}


def _send_notice(turn: _Turn, saved: TaskState, act: Notify) -> None:
    ctx = _NOTICES.get(act.template, _plain)(turn, saved, act)
    msg_id = turn.deps.notifier.send(act.template, issue=saved.issue, title=saved.title,
                                     url=_url(turn.repository, saved.issue),
                                     repository=turn.repository.name, **ctx)
    if act.template in _TAIL_NOTICES:
        turn.msg_id = msg_id


def _rounds_reset(turn: _Turn, task: TaskState, act: ResetRounds) -> TaskState:
    return loops.reset(task, act.cause)


def _override_set(turn: _Turn, task: TaskState, act: SetResumeOverride) -> TaskState:
    """The model the next wake runs on, whether it launches past the usage
    gate, and what a resume wake's launch tells the session."""
    return replace(task, resume_model_override=act.model, resume_bypass_usage=act.bypass_usage,
                   resume_text=act.text)


def _card_released(turn: _Turn, task: TaskState, act: ReleaseCard) -> TaskState:
    _release_card(turn.deps, turn.repository, task.issue)
    return task


def _issue_canceled(turn: _Turn, task: TaskState, act: CancelIssue) -> TaskState:
    _cancel_issue(turn.deps, turn.repository, task.issue)
    return task


def _merge_finished(turn: _Turn, task: TaskState, act: FinishMerged) -> TaskState:
    """Before the commit and before the pane is ended, so a raise leaves
    the task as it was (parked on its PR, or its address-review session
    still running, never read as crashed). The issue close comes first (an issue the operator closed, even as not
    planned, is left as it is), then the board (a board without Done is
    skipped with a warning), then the teardown."""
    deps, repository = turn.deps, turn.repository
    if not turn.dry_run and deps.github.issue_state(repository.repo, task.issue) == "OPEN":
        deps.github.close_issue(repository.repo, task.issue)
    try:
        deps.github.set_status(repository, task.issue, "Done")
    except BoardError as exc:  # no Done on the board: a retry cannot help
        print(f"[warn] {repository.name}: board not updated for merged "
              f"#{task.issue} — {exc}", file=sys.stderr)
    if not turn.dry_run:
        task_artifacts.collect(turn.cfg.state_dir, task, repository.repo, auth=deps.github.auth)
        task_artifacts.pin_published(turn.cfg.state_dir, task, repository.repo)
    remove_workspace(repository, task.worktree, task.branch, dry_run=turn.dry_run)
    deps.github.delete_branch(repository, task.branch)
    turn.after.append(lambda saved: eventlog.append_event(
        turn.cfg.state_dir, "merged", repository=saved.repository, issue=saved.issue,
        stage="done", detail=f"PR #{saved.pr_number}"))
    return replace(task, done_at=_now())


def _tombstone_swept(turn: _Turn, task: TaskState, act: SweepTombstone) -> TaskState:
    """After the commit the ended task's file goes, so its ranked card is
    claimed fresh."""
    def sweep(saved: TaskState) -> None:
        delete(turn.cfg.state_dir, saved.repository, saved.issue)
        eventlog.append_event(turn.cfg.state_dir, "reopened", repository=saved.repository,
                              issue=saved.issue,
                              detail=f"{saved.status.kind.value} task ranked again")
    turn.after.append(sweep)
    return task


def _flushed(turn: _Turn, task: TaskState, act: Flush) -> TaskState:
    """The worktree and local branch go (the remote branch, PR and issue are
    the record), then, after the commit, the state file. A done task's
    teardown already ran at merge; remove_workspace is best-effort, so
    repeating it is a no-op. A task whose repository left the config keeps
    its worktree: nothing knows its clone any more."""
    if turn.repository is not None and task.worktree:
        remove_workspace(turn.repository, task.worktree, task.branch, dry_run=turn.dry_run)

    def retire(saved: TaskState) -> None:
        _clear_wake_blocked(turn.cfg.state_dir, saved.repository, saved.issue)
        _rank_rejected_path(turn.cfg.state_dir, saved.repository, saved.issue).unlink(missing_ok=True)
        clear_session(turn.cfg.state_dir, saved.repository, saved.issue)
        delete(turn.cfg.state_dir, saved.repository, saved.issue)
        eventlog.append_event(turn.cfg.state_dir, "flushed", repository=saved.repository,
                              issue=saved.issue, stage=saved.stage.value)
    turn.after.append(retire)
    return task


# The actions a session event emits; the other event families wire theirs
# with their own tickets (#132).
_ACT: dict[type, Callable[..., TaskState]] = {
    AcquireSlot: _acquire_slot, ReleaseSlot: _release_slot, EndSession: _ended,
    RespawnStage: _respawned, RetryStage: _retried, PublishSpec: _published,
    ContinueSession: _session_continued, SpawnAddressReview: _address_review_spawned,
    ApplyRound: _counted, Poke: _poked, EndPokeStretch: _stretch_ended,
    RecordBackgroundWait: _wait_recorded, ReportCrash: _crash_reported,
    ReportTaskCrash: _task_crash_reported, MarkAsked: _marked_asked, EnterGate: _gate_entered,
    LinkPR: _pr_linked, ClearTurnMarkers: _markers_cleared, Notify: _noticed,
    WakeBlocked: _wake_blocked, ResetRounds: _rounds_reset, SetResumeOverride: _override_set,
    ReleaseCard: _card_released, CancelIssue: _issue_canceled, SweepTombstone: _tombstone_swept,
    Flush: _flushed, FinishMerged: _merge_finished,
}


def _report_session_crash(cfg: Config, deps: Deps, repository: Repository,
                          task: TaskState, dry_run: bool, log_tail: str) -> None:
    rep = failures.FailureReport(
        klass="session-crash", repository=repository.name, issue=task.issue,
        title=f"session crashed during {task.stage.value}: {task.title}",
        error=(f"session task-{task.repository}-{task.issue} died during stage "
               f"{task.stage.value}"),
        log_tail=log_tail,
        repro=_crash_repro(cfg, task),
        worktree=task.worktree)
    blocker = failures.report_failure(cfg, deps, rep, dry_run=dry_run)
    if blocker:
        _blocked_by(deps, repository, task, blocker)


def _crash_repro(cfg: Config, task: TaskState) -> str:
    # The stage's pick names its runtime. With no pick the session that
    # died is the worktree's last launch: a ticket is done and the next one
    # waits, so the implement pick is gone, and the done ticket's session,
    # still open, signalled `working` again (an operator attached to it)
    # before it died. A pre-picks task ran on Claude, which is what a bare
    # (here: empty) id resolves to.
    pick = (stage_pick(task.picks, task.stage.value)
            or _last_launched(task.worktree))
    runtime = runtime_for(parse_entry(pick, "pick").model_id if pick else "")
    record = read_session(cfg.state_dir, task.repository, task.issue)
    if record is None or record.stage != task.stage.value:
        return "Restart the stage from the dispatcher: no session recorded"
    return (f"cd {shlex.quote(task.worktree)} && "
            f"{runtime.resume_cmd(record.session_id)}  # inside session image")


def _blocked_by(deps: Deps, repository: Repository, task: TaskState, blocker: int) -> None:
    try:
        deps.github.append_blocked_by(repository, task.issue, blocker)
    except Exception as exc:
        print(f"[warn] append_blocked_by failed for #{task.issue}: {exc}",
              file=sys.stderr)


def _report_provisioning_failure(cfg: Config, deps: Deps, repository: Repository,
                                 cand, dry_run: bool) -> None:
    wt = str(Path(repository.worktrees_path) / f"task-{cand.number}")
    rep = failures.FailureReport(
        klass="provisioning", repository=repository.name, issue=cand.number,
        title=f"provisioning failed: {cand.title}",
        error=traceback.format_exc(),
        log_tail=failures.setup_log_tail(wt),
        repro=(f"podman run --rm -v {wt}:{wt} -w {wt} agent-ops-session "
               f"{repository.setup_cmd}" if repository.setup_cmd
               else "no setup_cmd; the worktree step failed (see error)"),
        worktree=wt)
    blocker = failures.report_failure(cfg, deps, rep, dry_run=dry_run)
    # Quarantine only once the report exists (marker written) — a gh outage
    # leaves neither, so the next pass retries both. blocker may still be 0
    # when infra_repo is unset; that record blocks until manually deleted.
    if not dry_run and failures.reported(cfg.state_dir, rep):
        failures.write_quarantine(cfg.state_dir, repository.name, cand.number,
                                  blocker_repo=cfg.infra_repo if blocker else "",
                                  blocker_issue=blocker,
                                  fp=failures.fingerprint(rep))


def _release_card(deps: Deps, repository: Repository, issue: int) -> None:
    """A kill: the card goes back to Ready; a GitHub failure only warns."""
    try:
        deps.github.release(repository, issue, "abandoned by operator")
    except Exception as exc:
        print(f"[warn] release failed while killing #{issue}: {exc}",
              file=sys.stderr)


def _cancel_issue(deps: Deps, repository: Repository, issue: int) -> None:
    """A cancel: the card to Wont do, the issue closed as not planned; a
    GitHub failure only warns."""
    try:
        deps.github.cancel(repository, issue)
    except Exception:
        log.warning("github cancel failed for %s/#%d", repository.name, issue,
                    exc_info=True)
