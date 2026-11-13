"""classify: the one session event a pass raises for a driven task, from
what the pass observed of it (ADR 0014, design "One event per driven task
per pass"). decide then answers that event."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from dispatcher.artifacts import CheckResult, PlanRevision
from dispatcher.lifecycle import (Death, Event, EventType, Gate, Report, Silence, Stranded,
                                  Wait)
from dispatcher.loops import (Decision, Loop, ReportedRound, Retry, evaluate, retry_left,
                              unattended_rounds_used)
from dispatcher.loops import Outcome as Rounds
from dispatcher.state import (BackgroundWait, LoopCaps, OperatorRequest, Stage, StageSignal,
                              TaskState)


class BackgroundView(NamedTuple):
    """What a pass knows about a task's background wait."""
    wait: BackgroundWait
    agent: tuple[str, int] | None   # herdr (status, state-change counter); None = unknown
    now: float
    cap: int                        # background_wait_seconds


class Reads(NamedTuple):
    """What the worktree says about a report, read by the executor."""
    tickets: CheckResult | None = None
    revision: PlanRevision | None = None    # of the report's review page
    spec: CheckResult | None = None
    page_questions: int | None = None       # question blocks on the prescribed page it names
    answers: tuple[str, str] | None = None  # (path, fingerprint) of an answers page


class Observation(NamedTuple):
    """Everything a pass saw of one driven task; classify reads only this."""
    task: TaskState
    signal: StageSignal | None
    alive: bool
    waiting: bool                   # the session ended its turn waiting for input
    idle: float | None
    stall_after: float              # 0: liveness detection is off
    background: BackgroundView | None
    now: float
    poke_cap: int
    gone: str = ""                  # why the task's track cannot run
    grace_elapsed: bool = False
    caps: LoopCaps = LoopCaps()
    tracks: frozenset[str] | None = None   # None: a done report's track is not checked
    gate_free: frozenset[str] = frozenset()
    reads: Reads = Reads()


def config_note(why: str) -> str:
    """The question of a task the config strands."""
    return (f"{why}; restore it in the console's model settings or targets.yaml, "
            f"or run the task with a model override")


def classify(o: Observation) -> Event | None:
    """The one session event of this pass, by precedence (_PRECEDENCE): a
    track gone; a background wait in force; a dead session (unless it left a
    `done`); a silent session past the stall time; a stopped session (its
    waiting marker); the gate's grace time; the stage signal. None: nothing
    happened. A waiting marker outranks a background marker (no wait is in
    force), and a wait in force holds off the stall."""
    return next((e for e in (rule(o) for rule in _PRECEDENCE) if e is not None), None)


def _working_signal(o: Observation) -> bool:
    return o.signal is not None and o.signal.status == "working"


def _track_gone(o: Observation) -> Event | None:
    if not o.gone:
        return None
    return Event(EventType.TRACK_UNCONFIGURED, Stranded(note=config_note(o.gone), login=""))


def _in_background(o: Observation) -> Event | None:
    """A background marker decides the pass only for a live session whose
    signal says working and that left no waiting marker; None also once
    herdr's counter moved (a new turn: today's rules decide)."""
    if o.background is None or not o.alive or o.waiting or not _working_signal(o):
        return None
    held = _wait_facts(o.task, o.background)
    if held is None:
        return None
    note, record = held
    return Event(EventType.SESSION_WAITING,
                 Wait(round=_round(o), note=note, record=record, login=""))


def _wait_facts(t: TaskState, view: BackgroundView
                ) -> tuple[str, tuple[float, int] | None] | None:
    """(why the wait parks, the report to record) while the wait is in
    force; None once herdr's counter moved."""
    if view.agent is None:
        return "", None   # herdr cannot be asked: hold
    status, seq = view.agent
    if view.wait.reported != t.background_reported:
        return "", None if status == "working" else (view.wait.reported, seq)
    if seq != t.background_seq:
        return None
    if view.now - view.wait.since > view.cap:
        return f"(background work still running after {view.cap // 60}m — cap reached)", None
    return "", None


def _dead(o: Observation) -> Event | None:
    if o.alive or (o.signal is not None and o.signal.status == "done"):
        return None
    t = o.task
    return Event(EventType.SESSION_DIED, Death(
        at_gate=t.stage is Stage.AWAITING_PLAN_REVIEW, rounds_used=unattended_rounds_used(t),
        launch_denied=False))


def _past_stall(o: Observation) -> bool:
    return o.alive and o.stall_after > 0 and o.idle is not None and o.idle > o.stall_after


def _silent(o: Observation) -> Event | None:
    """No output past the stall time; gated statuses are idle by design."""
    if not _past_stall(o) or (o.signal is not None and (o.waiting or not _working_signal(o))):
        return None
    note = (f"(no session output for {int(o.stall_after) // 60}m — "
            f"likely login/trust prompt or hang)")
    return Event(EventType.SESSION_IDLE, Silence(round=None, note=note, pane=None, login=""))


def _at_composer(o: Observation) -> bool:
    """Live, waiting marker, signal working, herdr not working (idle above
    0), liveness detection on."""
    return (_working_signal(o) and o.waiting and o.alive and o.stall_after > 0
            and bool(o.idle))


def _poke_cap_note(o: Observation) -> str:
    """Why the pokes stop: the first of this stretch is older than the cap."""
    t = o.task
    if not t.poked_since or o.now - t.poked_since <= o.poke_cap:
        return ""
    return (f"(poked {t.pokes} times over {o.poke_cap // 60}m — "
            f"stopped mid-stage without a stage signal)")


def _stopped(o: Observation) -> Event | None:
    """A live session that ended its turn mid-stage: poked on every pass
    until the cap."""
    if not _at_composer(o):
        return None
    return Event(EventType.SESSION_IDLE,
                 Silence(round=_round(o), note=_poke_cap_note(o), pane=None, login=""))


def _grace(o: Observation) -> Event | None:
    """An armed plan whose review the operator left past the grace time."""
    if (o.signal is None or o.signal.status != "awaiting-review"
            or not o.grace_elapsed or not _gate(o).armed):
        return None
    return Event(EventType.GRACE_EXPIRED)


def _reported(o: Observation) -> Event | None:
    return Event(EventType.SESSION_SIGNAL, _report(o)) if o.signal else None


_PRECEDENCE = (_track_gone, _in_background, _dead, _silent, _stopped, _grace, _reported)


def _round(o: Observation) -> Decision | None:
    """The loop round a working report names, when the policy counts it."""
    if not _working_signal(o):
        return None
    try:
        loop = Loop(o.signal.loop)
    except ValueError:
        return None
    d = evaluate(o.task, ReportedRound(loop, o.signal.round), o.caps)
    return None if d.outcome is Rounds.UNCHANGED else d


def _gate(o: Observation) -> Gate:
    t = o.task
    return Gate(at=t.stage is Stage.AWAITING_PLAN_REVIEW, gated=t.gated,
                rounds_used=unattended_rounds_used(t), respawn_pending=t.gate_respawn_pending,
                armed=_armed(t.operator_request, o.reads.revision))


def _report(o: Observation) -> Report:
    t, r, gate = o.task, o.reads, _gate(o)
    return Report(
        signal=o.signal, round=_round(o), gate=gate,
        retries=frozenset(x for x in Retry if retry_left(t, x)),
        asks=t.stage in (Stage.SPEC, Stage.PLAN),
        bad_track=_bad_track(o.signal, o.tracks, optional=gate.at),
        skips_gate=_skips_gate(o), tickets=r.tickets, revision=r.revision, spec=r.spec,
        answers=r.answers, launch_denied=False, login="")


def _armed(request: OperatorRequest | None, revision: PlanRevision | None) -> bool:
    """Is the request armed for the plan revision on disk? A request with no
    fingerprint is for no plan, and a revision that cannot be told matches
    no request."""
    return (request is not None and revision is not None and bool(revision.fingerprint)
            and (request.path, request.fingerprint) == (revision.path, revision.fingerprint))


def _skips_gate(o: Observation) -> bool:
    """The gate skip, decided by the dispatcher alone: a task that never
    waited at the gate, on a gate-free track, no questionnaire asked, and a
    count of 0 question blocks that the prescribed review page confirms."""
    t = o.task
    return (not t.gated and t.track in o.gate_free and not t.asked
            and o.signal is not None and o.signal.open_questions == 0
            and o.reads.page_questions == 0)


def _bad_track(signal: StageSignal | None, tracks: frozenset[str] | None,
               optional: bool) -> str:
    """Why a report's track is refused; "" when it is a configured one, when
    validation is off, or when it is left out where `optional` (an approval:
    the task keeps its track)."""
    if signal is None or tracks is None or signal.track in tracks:
        return ""
    if optional and not signal.track:
        return ""
    return f"stage.json names track {signal.track!r}; it must be one of {sorted(tracks)}"


def artifact_path(t: TaskState, signal: StageSignal) -> Path:
    """The signal's artifact, resolved against the worktree."""
    p = Path(signal.artifact)
    return p if p.is_absolute() else Path(t.worktree) / p
