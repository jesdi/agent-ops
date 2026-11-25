"""Pure view models: TaskState + rank rows + usage + failures + events ->
Pydantic responses. NO I/O in this module — construction only."""
from __future__ import annotations

import sys

from datetime import datetime, timedelta
from pathlib import Path

from typing import Annotated, Collection, Literal, Mapping, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from dispatcher import messages as msgq
from dispatcher import priority
from dispatcher.claims import (claim_room, pick_project, project_counts,
                               project_last_claims)
from dispatcher.config import Project, Repository
from dispatcher.models import Entry, Order
from dispatcher.usage import (PaceConfig, ProviderUsage, Reading, Source,
                              WindowKind, admits, minutes_to_reset, readings,
                              required_pace, verdict_note)
from dispatcher.state import Stage, StageSignal, TaskState, max_slots
from dispatcher.status import Cause, HumanReason, Kind, Status, crashed, ended, occupies

Zone = Literal["needs-you", "pipeline"]

# (key, title, zone) in display order — the single place column semantics
# live. Needs you first, in action order: a review is a read, a PR a merge, a
# park usually a short reply, a failure an investigation, a budget stall a
# wait. Then the pipeline in flow order. Consumers render this order as-is.
COLUMNS: tuple[tuple[str, str, Zone], ...] = (
    ("needs-review", "Needs review", "needs-you"),
    ("pr-open", "PR review", "needs-you"),
    ("parked", "Parked", "needs-you"),
    ("failed", "Failed", "needs-you"),
    ("stalled", "Stalled on budget", "needs-you"),
    ("queued", "Queued", "pipeline"),
    ("in-progress", "In progress", "pipeline"),
    ("awaiting-ci", "Awaiting CI", "pipeline"),
    ("resuming", "Resuming", "pipeline"),
    ("done", "Done", "pipeline"),
    ("wont-do", "Wont do", "pipeline"),
)

# The console's fields of a task until the web reads the status (#132
# ticket 13): what the task file stored before the status, derived from it.
PARK_HUMAN = "parked"
PARK_CI = "awaiting-ci"
PARK_WAKE = "unpark-requested"
PARK_LOGIN = "parked-login"
PARK_REVIEW = "awaiting-review"
GATE_STAGE = "awaiting-plan-review"

# A login park shares the Parked column: it is a task waiting on the operator,
# and the card keeps the exact park kind so the board can tell them apart.
# Without the mapping it fell through to the stage column and a session stuck
# at a /login prompt rendered as healthy "In progress" work.
# A review park maps to Needs review — a finished plan waiting for a human is
# exactly that column's meaning, parked or not.
_PARK_COLUMN = {PARK_HUMAN: "parked", PARK_CI: "awaiting-ci",
                PARK_WAKE: "resuming", PARK_LOGIN: "parked",
                PARK_REVIEW: "needs-review"}
_STAGE_COLUMN = {
    Stage.QUEUED.value: "queued",
    Stage.SPEC.value: "in-progress",
    Stage.PLAN.value: "in-progress",
    Stage.IMPLEMENT.value: "in-progress",
    Stage.REVIEW.value: "in-progress",
    GATE_STAGE: "needs-review",
    Stage.PR_OPEN.value: "pr-open",
    Stage.ADDRESS_REVIEW.value: "in-progress",
    Kind.DONE.value: "done",
    Kind.FAILED.value: "failed",
    Kind.CANCELED.value: "wont-do",
}


class Shown(NamedTuple):
    """A task's console fields as the park fields named them."""
    stage: str
    park: str
    park_reason: str
    park_note: str


# Each park kind's park field, and the reason the card shows for it.
_PARKS = {Kind.HELD_LOGIN: (PARK_LOGIN, "login"), Kind.PARKED_CI: (PARK_CI, ""),
          Kind.PARKED_REVIEW: (PARK_REVIEW, ""), Kind.PARKED_ACCESS: (PARK_HUMAN, "access"),
          Kind.PARKED_ANSWERS: (PARK_HUMAN, "decision")}
_WHY = {HumanReason.LOOP_CAP: "loop cap", HumanReason.BAD_SIGNAL: "bad signal"}
_AT_GATE = frozenset({Kind.HELD_PLAN_APPROVAL, Kind.PARKED_REVIEW})
# The wakes a pass launches on its own: no park, and the card stays where the
# task is (queued, review, pr-open).
_UNPARKED_WAKES = frozenset({Cause.LAUNCH, Cause.NEXT_STAGE, Cause.ATTENTION_FEEDBACK,
                             Cause.ATTENTION_CHECK_FAILED, Cause.ATTENTION_CONFLICT,
                             Cause.ATTENTION_OPERATOR})


def shown(t: TaskState) -> Shown:
    status = t.status
    if ended(status):
        return Shown(status.kind.value, "", "", "")
    stage = GATE_STAGE if _gate_family(status) else t.stage.value
    return Shown(stage, *_park_fields(status))


def _park_fields(status: Status) -> tuple[str, str, str]:
    """(park, park_reason, park_note) of a task that has not ended."""
    if status.kind is Kind.WAKING:
        return _woken_fields(status)
    if status.kind is Kind.PARKED_HUMAN:
        return PARK_HUMAN, _WHY.get(status.reason, status.reason.value), status.note
    park, reason = _PARKS.get(status.kind, ("", ""))
    return park, reason, status.note if park else ""


def _woken_fields(status: Status) -> tuple[str, str, str]:
    """A wake keeps the question it answers; no reason is shown for it."""
    if status.cause in _UNPARKED_WAKES:
        return "", "", ""
    return PARK_WAKE, "", status.origin.note


def _gate_family(status: Status) -> bool:
    """Held or parked at the plan gate, or woken from there."""
    origin = status.origin if status.kind is Kind.WAKING else status
    return origin is not None and origin.kind in _AT_GATE


def feedback_pending(t: TaskState) -> bool:
    """Woken by its PR's attention, the round not yet launched."""
    return t.status.kind is Kind.WAKING and t.status.cause in _UNPARKED_WAKES - {
        Cause.LAUNCH, Cause.NEXT_STAGE}


def column_for(stage: str, park: str) -> str:
    if park in _PARK_COLUMN:
        return _PARK_COLUMN[park]
    return _STAGE_COLUMN[stage]


class MessageView(BaseModel):
    id: str
    text: str
    actor: str
    created_at: str
    delivered_at: str          # "" while queued
    state: str                 # sending | queued | delivered


def message_views(msgs: list[msgq.Message],
                  pending: list[dict]) -> list[MessageView]:
    """The thread the console renders. Two sources, deliberately:
    `msgs` is the dispatcher-owned queue file, `pending` is the intents dir —
    a reply the web has written but no pass has drained yet. "sending" always
    sorts last: it is by construction the newest thing the operator did, and
    an intent's created_at can be missing or clock-skewed.

    A textless pending intent is not a message: the Resume button posts `{}`,
    so its intent carries text="" and would otherwise render as an empty
    "sending" bubble for something the operator never typed. (The queue file
    cannot contain one — _queue_message drops blank text.)"""
    out = [MessageView(id=m.id, text=m.text, actor=m.actor,
                       created_at=m.created_at,
                       delivered_at=m.delivered_at,
                       state="delivered" if m.delivered_at else "queued")
           for m in msgs]
    out += [MessageView(id=str(p.get("id", "")), text=str(p.get("text", "")),
                        actor=str(p.get("actor", "")),
                        created_at=str(p.get("created_at", "")),
                        delivered_at="", state="sending")
            for p in pending if str(p.get("text", "")).strip()]
    return out


def delivery_contract(t: TaskState | None, *, wake_blocked: bool) -> str:
    """What the compose box promises BEFORE the operator hits send. Derived
    from task state so it can never contradict what the dispatcher will
    actually do with the message."""
    if t is None:
        return "will deliver when this task is claimed"
    if crashed(t.status):
        return "will deliver when you resume this task"
    if ended(t.status):
        return "will deliver if this task restarts"
    if shown(t).park:
        if wake_blocked:
            return ("will deliver when the session resumes — waiting for a "
                    "free slot")
        return "will deliver when the session resumes"
    return ("will deliver at the next session boundary — this session is "
            "still running")


class ModelAdmissionView(BaseModel):
    model: str
    provider: str
    admitted: bool
    note: str


class TaskAdmissionView(BaseModel):
    """Requested model and server-filtered, eligible alternatives."""
    requested: ModelAdmissionView
    alternatives: list[ModelAdmissionView]
    # The pinned track the waiting launch comes from; "" when none.
    pinned_track: str = ""


class TaskCard(BaseModel):
    issue: int
    repository: str
    # From the config, not the task: "" once the repository left the config.
    project: str
    title: str
    stage: str
    park: str
    park_note: str
    # Why the task sits in Parked: a WHY_* value, "login" for a login park,
    # "" for every other park and for a park from before the field.
    park_reason: str
    column: str
    slot: int
    branch: str
    model: str
    track: str
    # The pinned track the next launch comes from; "" when it is not pinned.
    pinned_track: str = ""
    park_note_pending: bool
    feedback_pending: bool
    updated_at: str
    # Whether this card holds one of the dispatcher's capacity units. Derived
    # server-side so the console cannot disagree with the dispatcher about a
    # rule with a real exception (parked-login still counts).
    consuming_capacity: bool
    # From the event log; "" / None when the claimed event rotated away.
    claimed_at: str
    cycle_seconds: float | None
    # Backlog rank score (impact/effort) looked up from the rank rows; None
    # once the task drops off the ranking (typically Done/Failed).
    score: float | None
    # Queued (undelivered) messages on this issue — the ✉ badge.
    undelivered_messages: int = 0
    # A wake this task asked for was denied for want of capacity or a slot.
    wake_blocked: bool = False
    # Present only while a queued wake is denied by its model's usage gate.
    admission: TaskAdmissionView | None = None


class GhostCard(BaseModel):
    """A ranked, not-yet-claimed candidate: exactly what _claim_new would
    consume next, rendered in the Queued column ahead of being claimed."""
    number: int
    repository: str
    project: str  # the project whose queue ranked it
    title: str
    url: str
    score: float | None
    boost: int
    admission: TaskAdmissionView | None = None


class Column(BaseModel):
    key: str
    title: str
    zone: Zone
    cards: list[TaskCard]
    # Ranked candidates behind the cards. Only Queued has any, and only on the
    # full board: the snapshot never runs the ranking.
    ghosts: list[GhostCard] = []


class CapacityView(BaseModel):
    active: int
    capacity: int
    slots_used: int
    max_slots: int
    slots_held: list[int] = []


class BoardSnapshot(BaseModel):
    columns: list[Column]
    capacity: CapacityView
    median_cycle_seconds: float | None


class BoardView(BoardSnapshot):
    # The ranking behind Queued's ghosts failed and a cached one is shown.
    queue_stale: bool
    next_claim: NextClaimView
    usage: UsageView


def task_card(t: TaskState, *, model: str,
              claimed_at: str = "",
              cycle_seconds: float | None = None,
              score: float | None = None,
              undelivered_messages: int = 0,
              wake_blocked: bool = False,
              admission: TaskAdmissionView | None = None,
              pinned_track: str = "",
              project: str = "") -> TaskCard:
    fields = shown(t)
    return TaskCard(
        issue=t.issue, repository=t.repository, project=project, title=t.title,
        stage=fields.stage, park=fields.park,
        park_note=fields.park_note,
        park_reason=fields.park_reason,
        column=column_for(fields.stage, fields.park),
        slot=t.slot, branch=t.branch, model=model, track=t.track,
        pinned_track=pinned_track,
        # PARK_HUMAN only: it is the one park whose Telegram ping may be
        # missing, and the console is then the only way to answer it. A login
        # park always has a message id (a failed send degrades to PARK_HUMAN),
        # and CI/wake parks ask the operator nothing.
        park_note_pending=(fields.park == PARK_HUMAN and t.park_msg_id == 0),
        feedback_pending=feedback_pending(t),
        updated_at=t.updated_at,
        consuming_capacity=occupies(t.status),
        claimed_at=claimed_at, cycle_seconds=cycle_seconds, score=score,
        undelivered_messages=undelivered_messages,
        wake_blocked=wake_blocked, admission=admission)


def project_name(projects: Mapping[str, Project], repository: str) -> str:
    """`repository`'s project name; "" when it is no longer in the config."""
    project = projects.get(repository)
    return project.name if project else ""


def _score_index(queues: list[tuple[str, list[dict]]]
                 ) -> dict[tuple[str, int], float | None]:
    return {(row["repository"], row["number"]): row.get("score")
            for _project, rows in queues for row in rows}


def _task_cards(tasks: list[TaskState], *,
                models: dict[tuple[str, int], str],
                claimed: dict[tuple[str, int], str],
                scores: dict[tuple[str, int], float | None],
                mail: dict[tuple[str, int], int],
                blocked: set[tuple[str, int]],
                admissions: dict[tuple[str, int], TaskAdmissionView],
                pinned: dict[tuple[str, int], str],
                projects: Mapping[str, Project]
                ) -> list[TaskCard]:
    cards = []
    for task in tasks:
        key = (task.repository, task.issue)
        at = claimed_at(claimed, task.repository, task.issue)
        cards.append(task_card(
            task, model=models.get(key, ""), claimed_at=at,
            cycle_seconds=cycle_seconds(at, task.done_at),
            score=scores.get(key), undelivered_messages=mail.get(key, 0),
            wake_blocked=key in blocked, admission=admissions.get(key),
            pinned_track=pinned.get(key, ""),
            project=project_name(projects, task.repository)))
    return cards


def _cards_by_column(cards: list[TaskCard]) -> dict[str, list[TaskCard]]:
    by_column: dict[str, list[TaskCard]] = {key: [] for key, _, _ in COLUMNS}
    for card in cards:
        by_column[card.column].append(card)
    for col_cards in by_column.values():
        col_cards.sort(key=lambda card: (
            card.score is None, -(card.score or 0.0), card.issue))
    return by_column


def _held_slots(tasks: list[TaskState]) -> list[int]:
    return sorted({task.slot for task in tasks if occupies(task.status)})


def build_board_snapshot(tasks: list[TaskState], *, capacity: int,
                         models: dict[tuple[str, int], str], events: list[dict],
                         queues: list[tuple[str, list[dict]]],
                         undelivered: dict[tuple[str, int], int] | None = None,
                         wake_blocked: set[tuple[str, int]] | None = None,
                         admissions: dict[tuple[str, int], TaskAdmissionView] | None = None,
                         ghosts: list[GhostCard] | None = None,
                         pinned_tracks: dict[tuple[str, int], str] | None = None,
                         projects: Mapping[str, Project] | None = None,
                         onboarding_running: int = 0
                         ) -> BoardSnapshot:
    """Cards and capacity without any live-service dependencies; `ghosts`
    join the Queued column."""
    cards = _task_cards(tasks, models=models, claimed=claimed_at_index(events),
                        scores=_score_index(queues), mail=undelivered or {},
                        blocked=wake_blocked or set(), admissions=admissions or {},
                        pinned=pinned_tracks or {},
                        projects=projects or {})
    return BoardSnapshot(
        columns=_columns(_cards_by_column(cards), ghosts),
        capacity=_capacity_view(tasks, capacity, onboarding_running),
        median_cycle_seconds=median_cycle_seconds(events))


def _columns(by_column, ghosts: list[GhostCard] | None) -> list[Column]:
    """The board's columns in order; the ghosts join the Queued one."""
    return [Column(key=key, title=title, zone=zone, cards=by_column[key],
                   ghosts=(ghosts or []) if key == "queued" else [])
            for key, title, zone in COLUMNS]


def _capacity_view(tasks: list[TaskState], capacity: int,
                   onboarding_running: int) -> CapacityView:
    # Which numbers, not just how many: the console colours a card by its
    # slot, and the gauge must light the same segments. slots_used is the
    # LENGTH of that list, never a second count — counting `slot != NO_SLOT`
    # instead made old on-disk state (a parked task still recording a slot)
    # read "1/4" with zero segments lit.
    slots_held = _held_slots(tasks)
    return CapacityView(
        # via dispatcher.status.occupies so the console never shows a
        # different capacity than the one the dispatcher enforces
        active=sum(occupies(t.status) for t in tasks) + onboarding_running,
        capacity=capacity,
        slots_used=len(slots_held),
        max_slots=max_slots(capacity),
        slots_held=slots_held)


def _ghost_card(project: str, r: dict,
                admissions: Mapping[tuple[str, int], TaskAdmissionView]) -> GhostCard:
    return GhostCard(number=r["number"], repository=r["repository"],
                     project=project, title=r.get("title", ""), url=r.get("url", ""),
                     score=r.get("score"), boost=int(r.get("boost") or 0),
                     admission=admissions.get((r["repository"], r["number"])))


def _ghost_cards(tasks: list[TaskState], queues: list[tuple[str, list[dict]]],
                 admissions: Mapping[tuple[str, int], TaskAdmissionView]) -> list[GhostCard]:
    # Key on (repository, issue) so alpha#73 does not hide beta#73. Issue numbers
    # are per-repo; bare numbers would wrongly suppress cross-repository candidates
    # (cf. dispatcher/main.py:223 which acknowledges number collisions).
    known = {(t.repository, t.issue) for t in tasks}
    return [_ghost_card(project, r, admissions)
            for project, rows in queues for r in rows
            if _is_candidate(r) and (r["repository"], r["number"]) not in known]


def build_board(tasks: list[TaskState], *, capacity: int,
                models: dict[tuple[str, int], str],
                events: list[dict], heartbeat: dict | None, now: datetime,
                usage: UsageView,
                queues: list[tuple[str, list[dict]]],
                queue_stale: bool, claims_paused: bool,
                triage_running: bool,
                onboarding_running: int = 0,
                undelivered: dict[tuple[str, int], int] | None = None,
                wake_blocked: set[tuple[str, int]] | None = None,
                admissions: dict[tuple[str, int], TaskAdmissionView] | None = None,
                candidate_admissions: dict[tuple[str, int], TaskAdmissionView] | None = None,
                projects: Mapping[str, Project],
                last_claimed: Mapping[str, str],
                max_open: int = sys.maxsize,
                quarantined: Collection[tuple[str, int]] | None = None,
                pinned_tracks: dict[tuple[str, int], str] | None = None,
                ) -> BoardView:
    ghosts = _ghost_cards(tasks, queues, candidate_admissions or {})
    snapshot = build_board_snapshot(
        tasks, capacity=capacity, models=models, events=events, queues=queues,
        undelivered=undelivered, wake_blocked=wake_blocked,
        admissions=admissions, ghosts=ghosts, pinned_tracks=pinned_tracks,
        projects=projects, onboarding_running=onboarding_running)
    return BoardView(
        columns=snapshot.columns, capacity=snapshot.capacity,
        median_cycle_seconds=snapshot.median_cycle_seconds,
        queue_stale=queue_stale, usage=usage,
        next_claim=next_claim(heartbeat, now=now, tasks=tasks,
                              capacity=max(0, capacity - onboarding_running), gate=usage.gate,
                              queues=queues,
                              claims_paused=claims_paused,
                              triage_running=triage_running,
                              projects=projects,
                              last_claimed=last_claimed,
                              max_open=max_open,
                              quarantined=quarantined or ()))


class TaskDetail(BaseModel):
    card: TaskCard
    pane_tail: str
    session_alive: bool
    worktree: str
    messages: list[MessageView]
    delivery_contract: str
    ci_run_id: int
    effort: int | None
    labels: list[str]
    timeline: list[TimelineEntry]
    track_when: str = ""
    implement_progress: str | None = None


PROGRESS_MAX_CHARS = 200  # the session writes this text: bound what the page shows


def _implement_progress(t: TaskState,
                        signal: StageSignal | None) -> str | None:
    if t.stage != Stage.IMPLEMENT or signal is None \
            or signal.stage != Stage.IMPLEMENT or signal.status != "working":
        return None  # blocked / question / CI notes are not progress
    note = signal.note.strip()
    if len(note) > PROGRESS_MAX_CHARS:
        note = note[:PROGRESS_MAX_CHARS - 1].rstrip() + "…"
    return note or None


def task_detail(t: TaskState, *, model: str,
                pane_tail: str, session_alive: bool,
                signal: StageSignal | None = None,
                events: list[dict], now: datetime,
                messages: list[msgq.Message] | None = None,
                pending_sends: list[dict] | None = None,
                wake_blocked: bool = False,
                admission: TaskAdmissionView | None = None,
                track_when: str = "",
                pinned_track: str = "",
                project: str = "") -> TaskDetail:
    at = claimed_at(claimed_at_index(events), t.repository, t.issue)
    msgs = messages or []
    return TaskDetail(
        card=task_card(t, model=model, pinned_track=pinned_track,
                       project=project,
                       claimed_at=at,
                       cycle_seconds=cycle_seconds(at, t.done_at),
                       undelivered_messages=len(
                           [m for m in msgs if not m.delivered_at]),
                       wake_blocked=wake_blocked, admission=admission),
        pane_tail=pane_tail, session_alive=session_alive,
        worktree=t.worktree,
        messages=message_views(msgs, pending_sends or []),
        delivery_contract=delivery_contract(t, wake_blocked=wake_blocked),
        ci_run_id=t.status.run_id, effort=t.effort, labels=list(t.labels),
        timeline=stage_timeline(events, t.repository, t.issue, now=now),
        track_when=track_when,
        implement_progress=_implement_progress(t, signal))


class ProjectView(BaseModel):
    name: str
    paused: bool
    repositories: list[str]  # repository names, in config order


class ProjectsView(BaseModel):
    projects: list[ProjectView]  # every project in config order


def projects_view(members: Mapping[str, list[Repository]]) -> ProjectsView:
    """`members` is dispatcher.config.by_project's grouping."""
    return ProjectsView(projects=[
        ProjectView(name=name, paused=repos[0].project.paused,
                    repositories=[r.name for r in repos])
        for name, repos in members.items()])


class IssueDescription(BaseModel):
    title: str
    body: str
    url: str
    fetched_at: str
    error: str  # "" = ok; non-empty = fetch failed and no cache existed



_MEDIA_TYPES = {".md": "text/markdown", ".markdown": "text/markdown",
                ".html": "text/html", ".htm": "text/html"}


def media_type_for(path: str) -> str:
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


class ReadableContent(BaseModel):
    kind: Literal["readable"] = "readable"
    path: str        # worktree-relative
    media_type: str
    text: str


class UnavailableContent(BaseModel):
    """File exists as a request but cannot be read: missing, non-UTF-8, or containment
    violation; or the request is armed for no revision, so nothing on it can be submitted."""
    kind: Literal["unavailable"] = "unavailable"
    path: str        # stored path (wt-relative or raw)
    # "file-missing" | "not-utf8" | "path-escapes-worktree" | "no-revision"
    reason: str


# The selections of a review page: a question id (or "<id>.note", or
# "track") to its value.
Answers = dict[str, StrictStr | list[StrictStr] | StrictBool]


class OperatorRequest(BaseModel):
    kind: Literal["plan-approval", "answers"]
    content: Annotated[ReadableContent | UnavailableContent, Field(discriminator="kind")]
    revision: str
    answers: Answers


class PaneHistory(BaseModel):
    text: str


Severity = Literal["ok", "close", "blocked"]


class WindowView(BaseModel):
    kind: WindowKind
    scope: str | None
    used: float
    allowance: float
    headroom: float
    minutes_to_reset: float
    severity: Severity     # "close" once headroom <= CLOSE_HEADROOM
    # What auto ranks on, to one decimal. None for a session window and for a
    # weekly window with no weighted time left.
    required_pace: float | None


class ProviderUsageView(BaseModel):
    provider: str
    source: Source
    windows: list[WindowView]


class GateView(BaseModel):
    """The usage verdict for the untracked track's first spec entry: what an
    idle box would spawn next, and the verdict the dispatcher's stall/resume
    pings key on."""
    model: str
    provider: str
    admitted: bool
    note: str                  # verdict_note: the binding window and its numbers
    minutes_to_reset: float    # of the binding window; 0 when there is none
    binding: WindowView | None
    # the usage Verdict's reason; "unavailable" = dead login. Server-side only.
    reason: str = Field("ok", exclude=True)


class PriorityView(BaseModel):
    mode: str            # "auto" or a routed provider
    options: list[str]   # "auto", then the routed providers sorted by name
    first: str           # the provider the mode puts first; "" when none is routed
    pinned: list[str] = []  # the tracks the mode does not apply to, in policy order


class AuthDarkView(BaseModel):
    since: str  # ISO timestamp the login went dark


class UsageView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    providers: list[ProviderUsageView]
    gate: GateView
    priority: PriorityView
    auth_dark: AuthDarkView | None = None


CLOSE_HEADROOM = 0.08


def _severity(headroom: float) -> Severity:
    return "blocked" if headroom <= 0 else "close" if headroom <= CLOSE_HEADROOM else "ok"


def _window_view(r: Reading, now: datetime, pace: PaceConfig) -> WindowView:
    required = required_pace(r.window, now, pace)
    return WindowView(kind=r.window.kind, scope=r.window.scope,
                      used=r.window.used, allowance=r.allowance,
                      headroom=r.headroom,
                      minutes_to_reset=minutes_to_reset(r.window, now),
                      severity=_severity(r.headroom),
                      required_pace=None if required is None else round(required, 1))


def model_admission_view(usages: Mapping[str, ProviderUsage], *, now: datetime,
                         pace: PaceConfig, model: str) -> ModelAdmissionView:
    verdict = admits(usages, model, now, pace)
    return ModelAdmissionView(model=model, provider=verdict.provider,
                              admitted=verdict.admitted,
                              note=verdict_note(verdict, now))


def _usage_provider_views(usages: Mapping[str, ProviderUsage], now: datetime,
                          pace: PaceConfig) -> list[ProviderUsageView]:
    return [ProviderUsageView(
        provider=name, source=usages[name].source,
        windows=[_window_view(r, now, pace)
                 for r in readings(usages[name], now, pace)])
        for name in sorted(usages)]


def _usage_priority_view(usages: Mapping[str, ProviderUsage], now: datetime,
                         pace: PaceConfig, mode: str, routed: Collection[str],
                         pinned: Collection[str], order: Order | None
                         ) -> PriorityView:
    ordering = order or priority.order(mode, usages, now, pace)
    ranked = ordering(tuple(
        Entry(p, "") for p in sorted(routed, key=lambda p: (p != "anthropic", p))))
    return PriorityView(
        mode=mode, options=[priority.AUTO, *sorted(routed)],
        first=ranked[0].provider if ranked else "",
        pinned=list(pinned))


def _usage_gate_view(verdict, default_model: str, now: datetime,
                     pace: PaceConfig) -> GateView:
    binding = _window_view(verdict.binding, now, pace) if verdict.binding else None
    return GateView(model=default_model, provider=verdict.provider,
                    admitted=verdict.admitted, note=verdict_note(verdict, now),
                    minutes_to_reset=binding.minutes_to_reset if binding else 0.0,
                    binding=binding, reason=verdict.reason)


def usage_view(usages: Mapping[str, ProviderUsage], *, now: datetime,
               pace: PaceConfig, default_model: str, mode: str,
               routed: Collection[str],
               pinned: Collection[str] = (),
               order: Order | None = None,
               auth_dark_since: str | None = None) -> UsageView:
    """Every provider's windows, sorted by provider name, one gate (the
    verdict for the policy default model), and what the priority `mode` is
    doing over the `routed` providers, minus the `pinned` tracks it never
    reorders. The mode changes no window's numbers.
    `first` is the dispatcher's own order over one model-less entry per routed
    provider: a provider-level summary on unscoped windows only, so it can
    differ from the entry a task gets when a model has its own weekly window.
    anthropic is written first, so it wins a tie and the no-pace case."""
    verdict = admits(usages, default_model, now, pace)
    return UsageView(
        providers=_usage_provider_views(usages, now, pace),
        gate=_usage_gate_view(verdict, default_model, now, pace),
        priority=_usage_priority_view(usages, now, pace, mode, routed, pinned,
                                      order),
        auth_dark=None if auth_dark_since is None else AuthDarkView(since=auth_dark_since))


class QuarantineEntry(BaseModel):
    repository: str
    task_issue: int
    blocker_repo: str
    blocker_issue: int
    fingerprint: str
    created_at: str
    blocker_open: bool | None
    readable: bool


class FingerprintEntry(BaseModel):
    fingerprint: str
    repo: str
    issue: int
    when: str


class FailuresView(BaseModel):
    quarantined: list[QuarantineEntry]
    fingerprints: list[FingerprintEntry]


class EventEntry(BaseModel):
    ts: str
    event: str
    repository: str
    issue: int
    stage: str
    model: str
    actor: str
    detail: str


class HistoryView(BaseModel):
    events: list[EventEntry]


class QueueRow(BaseModel):
    number: int
    title: str
    url: str
    status: str
    labels: list[str]
    blocked: bool
    score: float | None
    boost: int
    in_flight: bool


class RepositoryQueue(BaseModel):
    repository: str
    as_of: str
    stale: bool
    rows: list[QueueRow]


class QueueView(BaseModel):
    repositories: list[RepositoryQueue]


def repository_queue(name: str, rows: list[dict], as_of: str, stale: bool,
                     in_flight: set[int]) -> RepositoryQueue:
    return RepositoryQueue(
        repository=name, as_of=as_of, stale=stale,
        rows=[QueueRow(
            number=r["number"], title=r.get("title", ""),
            url=r.get("url", ""), status=r.get("status", ""),
            labels=list(r.get("labels") or []),
            blocked=bool(r.get("blocked", False)),
            score=r.get("score"), boost=int(r.get("boost") or 0),
            in_flight=r["number"] in in_flight) for r in rows])


class TimelineEntry(BaseModel):
    label: str      # stage value, or "parked"
    seconds: float
    kind: str       # "stage" | "parked"
    ongoing: bool = False


def _parse_ts(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def claimed_at_index(events: list[dict]) -> dict[tuple[str, int], str]:
    """(repository, issue) -> ts of its FIRST claimed event. events.jsonl rotates
    at a size cap, so a task may have no claimed event at all — absent means
    unknown.

    Keyed on the pair, not the bare number: issue numbers are per-repo, so a
    merged alpha#73 from weeks ago would otherwise win the setdefault over a
    freshly claimed beta#73 and report a month-old claim time. Log lines
    written before events carried `repository` land under ("", issue) and are read
    back as a fallback for any repository — see claimed_at()."""
    out: dict[tuple[str, int], str] = {}
    for e in events:
        if e.get("event") == "claimed" and e.get("issue"):
            out.setdefault((str(e.get("repository") or ""), e["issue"]),
                           e.get("ts", ""))
    return out


def claimed_at(index: dict[tuple[str, int], str], repository: str,
               issue: int) -> str:
    """Exact (repository, issue) match, else the legacy entry with no repository, else ""
    — never another repository's claim."""
    return index.get((repository, issue)) or index.get(("", issue), "")


def cycle_seconds(claimed_iso: str, done_iso: str) -> float | None:
    a, b = _parse_ts(claimed_iso), _parse_ts(done_iso)
    if a is None or b is None:
        return None
    try:
        if b < a:
            return None
    except TypeError:
        # Mixed-awareness comparison (one naive, one aware): zone is unknown,
        # so we cannot produce a meaningful duration — return None.
        return None
    return (b - a).total_seconds()


def median_cycle_seconds(events: list[dict], last: int = 20) -> float | None:
    claimed = claimed_at_index(events)
    cycles = [c for e in events
              if e.get("event") == "merged" and e.get("issue")
              if (c := cycle_seconds(
                  claimed_at(claimed, str(e.get("repository") or ""), e["issue"]),
                  e.get("ts", ""))) is not None]
    if not cycles:
        return None
    tail = sorted(cycles[-last:])
    n = len(tail)
    return tail[n // 2] if n % 2 else (tail[n // 2 - 1] + tail[n // 2]) / 2


_TIMELINE_EVENTS = {"claimed", "stage-started", "parked", "resumed", "merged"}


class NextClaimView(BaseModel):
    verdict: str          # will-claim|no-candidates|projects-paused|capacity-full|budget-blocked|auth-dark|claims-paused|unknown
    next_pass_eta: str    # ISO; "" when verdict == "unknown"
    next_issue: int = 0
    next_repository: str = ""
    minutes_to_reset: float = 0
    blocked_by: str = ""


def _is_candidate(r: dict) -> bool:
    """Mirror of GitHubClient.candidates: what the claim round would consume."""
    return (r.get("status") == "Ready" and not r.get("blocked")
            and "auto" in (r.get("labels") or []))


Blocker = Literal["dispatcher-down", "claims-paused", "auth-dark",
                  "budget-blocked", "projects-paused", "capacity-full"]


def local_blocker(heartbeat: dict | None, *, now: datetime,
                  tasks: list[TaskState], capacity: int, gate: GateView,
                  claims_paused: bool, triage_running: bool,
                  projects: Mapping[str, Project],
                  max_open: int = sys.maxsize) -> Blocker | None:
    """The shared local claim gate, shared by the console forecast and
    `agent-ops status`: the first blocker readable from local state, or None.
    Order: heartbeat, triage hold, usage (unavailable = auth-dark, any other
    denial = budget-blocked), every project paused, no claim room."""
    if not pass_clock(heartbeat, now).alive:
        return "dispatcher-down"
    if claims_paused:
        return "claims-paused"
    if not gate.admitted:
        return "auth-dark" if gate.reason == "unavailable" else "budget-blocked"
    if projects and all(p.paused for p in projects.values()):
        return "projects-paused"
    if claim_room(capacity, max_open, tasks, triage_running) <= 0:
        return "capacity-full"
    return None


class PassClock(NamedTuple):
    """Whether the dispatcher is passing (`alive`), and the next-pass countdown
    (`eta`, ISO; "" when there is none to show)."""
    alive: bool
    eta: str


def pass_clock(heartbeat: dict | None, now: datetime) -> PassClock:
    """Dead: no heartbeat, a malformed one, or one older than two of its gaps
    (the interval, or for a calendar schedule the gap to the `next_at` it
    recorded). A calendar schedule shows no countdown."""
    hb = heartbeat or {}
    finished = _parse_ts(hb.get("finished_at", ""))
    if hb.get("custom") is True:
        return PassClock(_calendar_alive(finished, _parse_ts(hb.get("next_at") or ""), now), "")
    eta = _interval_eta(finished, hb.get("interval_minutes"), now)
    return PassClock(eta is not None, eta or "")


def _calendar_alive(finished: datetime | None, due: datetime | None, now: datetime) -> bool:
    """A pass on record and not two gaps past it. Without a `next_at` (systemd could
    not walk the calendar) the gap is unknown: a recorded pass counts as alive."""
    try:
        return finished is not None and (due is None or now <= due + (due - finished))
    except TypeError:  # a naive stamp: staleness cannot be judged
        return False


def _interval_eta(finished: datetime | None, interval_minutes, now: datetime) -> str | None:
    """`finished` plus the interval, or None when either is missing or the pass is stale."""
    try:
        interval = int(interval_minutes or 0)
        # A naive finished_at against an aware now raises TypeError: the zone
        # is unknown, so staleness cannot be judged either.
        fresh = (finished is not None and interval > 0
                 and (now - finished).total_seconds() <= 2 * interval * 60)
    except (ValueError, TypeError):
        return None
    return (finished + timedelta(minutes=interval)).isoformat() if fresh else None


def _queue_heads(queues: list[tuple[str, list[dict]]],
                 tasks: list[TaskState],
                 quarantined: Collection[tuple[str, int]] = ()
                 ) -> dict[str, tuple[str, int]]:
    """project -> its front-of-queue candidate (repository, issue), over rows
    not already known as tasks. Key on (repository, issue) — same rationale as
    build_board: issue numbers are per-repo so alpha#73 must not shadow
    beta#73 in the claim forecast."""
    known = {(t.repository, t.issue) for t in tasks}
    heads = {}
    for name, rows in queues:
        mine = [(r["repository"], r["number"]) for r in rows
                if _is_candidate(r) and (r["repository"], r["number"]) not in known
                and (r["repository"], r["number"]) not in quarantined]
        if mine:
            heads[name] = mine[0]
    return heads


def _pick(names: list[str], tasks: list[TaskState],
          projects: Mapping[str, Project],
          last_claimed: Mapping[str, str]) -> str | None:
    """claims.pick_project over `names`, fed as the claim round feeds it:
    active tasks and last claims summed per project, its max_active."""
    project_of = {r: p.name for r, p in projects.items()}
    caps = {p.name: p.max_active for p in projects.values()}
    return pick_project(names, project_counts(names, tasks, project_of),
                        project_last_claims(last_claimed, project_of), caps)


def _unpaused_head_names(heads: Mapping[str, list[tuple[str, int]]],
                         projects: Mapping[str, Project]) -> list[str]:
    paused = {p.name for p in projects.values() if p.paused}
    return [name for name in heads if name not in paused]


def _next_claim_from_heads(heads: dict[str, tuple[str, int]], *,
                           blocker: Blocker | None, eta: str,
                           tasks: list[TaskState],
                           projects: Mapping[str, Project],
                           last_claimed: Mapping[str, str]) -> NextClaimView:
    if not heads:
        return NextClaimView(verdict="no-candidates", next_pass_eta=eta)
    names = _unpaused_head_names(heads, projects)
    if not names:
        return NextClaimView(verdict="projects-paused", next_pass_eta=eta)
    name = _pick(names, tasks, projects, last_claimed) if blocker is None else None
    if name is None:
        return NextClaimView(verdict="capacity-full", next_pass_eta=eta)
    repository, issue = heads[name]
    return NextClaimView(verdict="will-claim", next_pass_eta=eta,
                         next_issue=issue, next_repository=repository)


def _early_verdict(blocker: Blocker | None, gate: GateView,
                   eta: str) -> NextClaimView | None:
    """The forecast when the shared gate alone decides it, else None."""
    if blocker is None:
        return None
    if blocker == "dispatcher-down":
        return NextClaimView(verdict="unknown", next_pass_eta="")
    if blocker in ("claims-paused", "auth-dark"):
        return NextClaimView(verdict=blocker, next_pass_eta=eta)
    if blocker == "budget-blocked":
        return NextClaimView(verdict="budget-blocked", next_pass_eta=eta,
                             minutes_to_reset=gate.minutes_to_reset,
                             blocked_by=gate.note)
    return None


def next_claim(heartbeat: dict | None, *, now: datetime,
               tasks: list[TaskState], capacity: int,
               gate: GateView,
               queues: list[tuple[str, list[dict]]],
               claims_paused: bool = False,
               triage_running: bool = False,
               projects: Mapping[str, Project],
               last_claimed: Mapping[str, str],
               max_open: int = sys.maxsize,
               quarantined: Collection[tuple[str, int]] = ()) -> NextClaimView:
    """A forecast of what the claim round (dispatcher main._claim_new)
    consumes next, from data already on the board request. `queues` are each
    project's rank rows, `projects` maps repository -> its project, and
    `last_claimed` is claims.last_claims (per repository). The project comes
    from the same claims.pick_project the dispatcher uses, over box-wide free
    capacity. It is a PARTIAL mirror, deliberately: the gates it models are
    the ones readable from local state (heartbeat, usage, capacity,
    max_active, paused projects, the triage pause, the cached rank rows).

    The usage gate here is the policy default model's verdict, while the
    dispatcher gates each candidate on its own spec model: a candidate a rule
    routes to another model can be claimed while this says budget-blocked,
    or denied while this says will-claim.

    Quarantine is modelled from the dispatcher's verdict as of the last
    completed pass. A record written or a blocker closed after that pass shows
    up one pass later. Unreached candidates are absent: a project that got no
    turn because capacity ran out first, or whose board read failed, is not in
    the set, so the forecast treats its candidates as claimable, as before.

    Gates it does NOT model, so `will-claim` can still be wrong:
      * slot exhaustion (the round stops when allocate_slot returns None)
        and a create_workspace failure — only knowable at claim time.
      * queue movement since the rank rows were cached (rank_rows has a TTL,
        so the dispatcher's own pass may see a different head).
    """
    blocker = local_blocker(heartbeat, now=now, tasks=tasks, capacity=capacity,
                            gate=gate, claims_paused=claims_paused,
                            triage_running=triage_running, projects=projects,
                            max_open=max_open)
    eta = pass_clock(heartbeat, now).eta
    early = _early_verdict(blocker, gate, eta)
    if early:
        return early
    heads = _queue_heads(queues, tasks, quarantined)
    return _next_claim_from_heads(heads, blocker=blocker, eta=eta, tasks=tasks,
                                  projects=projects, last_claimed=last_claimed)


def _on_timeline(e: dict, repository: str, issue: int) -> bool:
    """A transition event of this (repository, issue); an event with no
    repository key predates the key and counts for any repository."""
    return (e.get("issue") == issue
            and (e.get("repository") or "") in ("", repository)
            and e.get("event") in _TIMELINE_EVENTS)


def stage_timeline(events: list[dict], repository: str, issue: int, *,
                   now: datetime) -> list[TimelineEntry]:
    """Closed segments between this issue's transition events, in order; the
    open tail (no merged yet) ends at `now` with ongoing=True. Sub-second
    segments (claimed -> immediate spec spawn) are dropped as noise.

    Keyed on (repository, issue), not the bare number: issue numbers are
    per-repo, so two repositories claiming the same issue number must not have
    their claimed/parked/resumed/merged events interleave into one garbled
    timeline. Log lines written before events carried `repository` land with no
    repository key and are read back for any repository — same fallback convention
    as claimed_at()/claimed_at_index()."""
    out: list[TimelineEntry] = []
    open_seg: tuple[str, str, datetime] | None = None  # (label, kind, start)

    def close(end: datetime, ongoing: bool = False) -> None:
        nonlocal open_seg
        if open_seg is None:
            return
        label, kind, start = open_seg
        try:
            secs = (end - start).total_seconds()
        except TypeError:
            # Mixed-awareness endpoints (one naive, one aware): the zone is
            # unknown, so the segment has no meaningful duration. DROP it —
            # same contract as cycle_seconds and next_claim, and never invent
            # a timezone for a naive timestamp.
            open_seg = None
            return
        if secs >= 1:
            out.append(TimelineEntry(label=label, seconds=secs, kind=kind,
                                     ongoing=ongoing))
        open_seg = None

    last_stage = ""
    for e in events:
        if not _on_timeline(e, repository, issue):
            continue
        ts = _parse_ts(e.get("ts", ""))
        if ts is None:
            continue
        kind = e["event"]
        close(ts)
        if kind == "claimed":
            open_seg = (Stage.QUEUED.value, "stage", ts)
        elif kind == "stage-started":
            last_stage = e.get("stage", "") or last_stage
            open_seg = (last_stage, "stage", ts)
        elif kind == "parked":
            last_stage = e.get("stage", "") or last_stage
            open_seg = ("parked", "parked", ts)
        elif kind == "resumed":
            open_seg = (e.get("stage", "") or last_stage, "stage", ts)
        elif kind == "merged":
            open_seg = None
    close(now, ongoing=True)
    return out
