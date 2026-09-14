"""Pure view models: TaskState + rank rows + usage + failures + events ->
Pydantic responses. NO I/O in this module — construction only."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, Field

from dispatcher import messages as msgq
from dispatcher.usage import (PaceConfig, ProviderUsage, Reading, Source,
                              WindowKind, admits, minutes_to_reset, readings,
                              verdict_note)
from dispatcher.state import (IN_FLIGHT_STAGES, NO_SLOT, PARK_CI,
                              PARK_HUMAN, PARK_LOGIN, PARK_REVIEW, PARK_WAKE,
                              Stage, TaskState, active, consumes_capacity,
                              holds_slot, max_slots)

FINISHED_STAGES = frozenset({Stage.DONE, Stage.FAILED, Stage.CANCELED})

# (key, title) in display order — the single place column semantics live.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("queued", "Queued"),
    ("in-progress", "In progress"),
    ("needs-review", "Needs review"),
    ("pr-open", "PR review"),
    ("done", "Done"),
    ("parked", "Parked"),
    ("awaiting-ci", "Awaiting CI"),
    ("resuming", "Resuming"),
    ("stalled", "Stalled on budget"),
    ("failed", "Failed"),
    ("wont-do", "Wont do"),
)

# PARK_LOGIN shares the Parked column: it is a task waiting on the operator,
# and the card keeps the exact park kind so the board can tell them apart.
# Without the mapping it fell through to the stage column and a session stuck
# at a /login prompt rendered as healthy "In progress" work.
# PARK_REVIEW maps to Needs review — a finished spec waiting for a human is
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
    Stage.AWAITING_SPEC_REVIEW.value: "needs-review",
    Stage.PR_OPEN.value: "pr-open",
    Stage.ADDRESS_REVIEW.value: "in-progress",
    Stage.DONE.value: "done",
    Stage.FAILED.value: "failed",
    Stage.BLOCKED.value: "failed",  # legacy stage, surfaced not hidden
    Stage.STALLED_ON_BUDGET.value: "stalled",
    Stage.CANCELED.value: "wont-do",
}


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
    if t.stage in FINISHED_STAGES:
        return "will deliver if this task restarts"
    if t.park:
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
    requested: ModelAdmissionView
    alternatives: list[ModelAdmissionView]


class TaskCard(BaseModel):
    issue: int
    target: str
    title: str
    stage: str
    park: str
    park_note: str
    column: str
    slot: int
    branch: str
    model: str
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


class Column(BaseModel):
    key: str
    title: str
    cards: list[TaskCard]


class GhostCard(BaseModel):
    """A ranked, not-yet-claimed candidate: exactly what _claim_new would
    consume next, rendered in the Queued column ahead of being claimed."""
    number: int
    target: str
    title: str
    url: str
    score: float | None
    boost: int
    admission: TaskAdmissionView | None = None


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
    upcoming: list[GhostCard]
    upcoming_stale: bool
    next_claim: NextClaimView


def task_card(t: TaskState, *, model: str,
              claimed_at: str = "",
              cycle_seconds: float | None = None,
              score: float | None = None,
              undelivered_messages: int = 0,
              wake_blocked: bool = False,
              admission: TaskAdmissionView | None = None) -> TaskCard:
    return TaskCard(
        issue=t.issue, target=t.target, title=t.title,
        stage=t.stage.value, park=t.park,
        park_note=t.park_note,
        column=column_for(t.stage.value, t.park),
        slot=t.slot, branch=t.branch, model=model,
        # PARK_HUMAN only: it is the one park whose Telegram ping may be
        # missing, and the console is then the only way to answer it. A login
        # park always has a message id (a failed send degrades to PARK_HUMAN),
        # and CI/wake parks ask the operator nothing.
        park_note_pending=(t.park == PARK_HUMAN and t.park_msg_id == 0),
        feedback_pending=t.feedback_pending,
        updated_at=t.updated_at,
        consuming_capacity=consumes_capacity(t),
        claimed_at=claimed_at, cycle_seconds=cycle_seconds, score=score,
        undelivered_messages=undelivered_messages,
        wake_blocked=wake_blocked, admission=admission)


def _score_index(queues: list[tuple[str, list[dict]]]
                 ) -> dict[tuple[str, int], float | None]:
    return {(name, row["number"]): row.get("score")
            for name, rows in queues for row in rows}


def _task_cards(tasks: list[TaskState], *,
                models: dict[tuple[str, int], str],
                claimed: dict[tuple[str, int], str],
                scores: dict[tuple[str, int], float | None],
                mail: dict[tuple[str, int], int],
                blocked: set[tuple[str, int]],
                admissions: dict[tuple[str, int], TaskAdmissionView]
                ) -> list[TaskCard]:
    cards = []
    for task in tasks:
        key = (task.target, task.issue)
        at = claimed_at(claimed, task.target, task.issue)
        cards.append(task_card(
            task, model=models.get(key, ""), claimed_at=at,
            cycle_seconds=cycle_seconds(at, task.done_at),
            score=scores.get(key), undelivered_messages=mail.get(key, 0),
            wake_blocked=key in blocked, admission=admissions.get(key)))
    return cards


def _cards_by_column(cards: list[TaskCard]) -> dict[str, list[TaskCard]]:
    by_column: dict[str, list[TaskCard]] = {key: [] for key, _ in COLUMNS}
    for card in cards:
        by_column[card.column].append(card)
    for col_cards in by_column.values():
        col_cards.sort(key=lambda card: (
            card.score is None, -(card.score or 0.0), card.issue))
    return by_column


def _held_slots(tasks: list[TaskState]) -> list[int]:
    in_flight = [task for task in tasks if task.stage in IN_FLIGHT_STAGES]
    return sorted({task.slot for task in in_flight
                   if holds_slot(task) and task.slot != NO_SLOT})


def build_board_snapshot(tasks: list[TaskState], *, capacity: int,
                         models: dict[tuple[str, int], str], events: list[dict],
                         queues: list[tuple[str, list[dict]]],
                         undelivered: dict[tuple[str, int], int] | None = None,
                         wake_blocked: set[tuple[str, int]] | None = None,
                         admissions: dict[tuple[str, int], TaskAdmissionView] | None = None
                         ) -> BoardSnapshot:
    """Cards and capacity without any live-service dependencies."""
    mail = undelivered or {}
    blocked = wake_blocked or set()
    task_admissions = admissions or {}
    claimed = claimed_at_index(events)
    cards = _task_cards(tasks, models=models, claimed=claimed,
                        scores=_score_index(queues), mail=mail,
                        blocked=blocked, admissions=task_admissions)
    by_column = _cards_by_column(cards)
    in_flight = [t for t in tasks if t.stage in IN_FLIGHT_STAGES]
    # Which numbers, not just how many: the console colours a card by its
    # slot, and the gauge must light the same segments. slots_used is the
    # LENGTH of that list, never a second count — counting `slot != NO_SLOT`
    # instead made old on-disk state (a parked task still recording a slot)
    # read "1/4" with zero segments lit.
    slots_held = _held_slots(tasks)
    return BoardSnapshot(
        columns=[Column(key=key, title=title, cards=by_column[key])
                 for key, title in COLUMNS],
        capacity=CapacityView(
            # via dispatcher.state.active so the console never shows a
            # different capacity than the one the dispatcher enforces
            active=len(active(in_flight)),
            capacity=capacity,
            slots_used=len(slots_held),
            max_slots=max_slots(capacity),
            slots_held=slots_held),
        median_cycle_seconds=median_cycle_seconds(events))


def build_board(tasks: list[TaskState], *, capacity: int,
                models: dict[tuple[str, int], str],
                events: list[dict], heartbeat: dict | None, now: datetime,
                gate: GateView,
                queues: list[tuple[str, list[dict]]],
                queue_stale: bool, claims_paused: bool,
                triage_running: bool,
                undelivered: dict[tuple[str, int], int] | None = None,
                wake_blocked: set[tuple[str, int]] | None = None,
                admissions: dict[tuple[str, int], TaskAdmissionView] | None = None,
                candidate_admissions: dict[tuple[str, int], TaskAdmissionView] | None = None
                ) -> BoardView:
    snapshot = build_board_snapshot(
        tasks, capacity=capacity, models=models, events=events, queues=queues,
        undelivered=undelivered, wake_blocked=wake_blocked,
        admissions=admissions)
    # Key on (target, issue) so alpha#73 does not hide beta#73. Issue numbers
    # are per-repo; bare numbers would wrongly suppress cross-target candidates
    # (cf. dispatcher/main.py:223 which acknowledges number collisions).
    known = {(t.target, t.issue) for t in tasks}
    ghost_admissions = candidate_admissions or {}
    upcoming = [GhostCard(number=r["number"], target=name,
                          title=r.get("title", ""), url=r.get("url", ""),
                          score=r.get("score"), boost=int(r.get("boost") or 0),
                          admission=ghost_admissions.get((name, r["number"])))
                for name, rows in queues for r in rows
                if _is_candidate(r) and (name, r["number"]) not in known]
    return BoardView(
        columns=snapshot.columns, capacity=snapshot.capacity,
        median_cycle_seconds=snapshot.median_cycle_seconds,
        upcoming=upcoming, upcoming_stale=queue_stale,
        next_claim=next_claim(heartbeat, now=now, tasks=tasks,
                              capacity=capacity, gate=gate,
                              queues=queues,
                              claims_paused=claims_paused,
                              triage_running=triage_running))


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


def task_detail(t: TaskState, *, model: str,
                pane_tail: str, session_alive: bool,
                events: list[dict], now: datetime,
                messages: list[msgq.Message] | None = None,
                pending_sends: list[dict] | None = None,
                wake_blocked: bool = False,
                admission: TaskAdmissionView | None = None) -> TaskDetail:
    at = claimed_at(claimed_at_index(events), t.target, t.issue)
    msgs = messages or []
    return TaskDetail(
        card=task_card(t, model=model,
                       claimed_at=at,
                       cycle_seconds=cycle_seconds(at, t.done_at),
                       undelivered_messages=len(
                           [m for m in msgs if not m.delivered_at]),
                       wake_blocked=wake_blocked, admission=admission),
        pane_tail=pane_tail, session_alive=session_alive,
        worktree=t.worktree,
        messages=message_views(msgs, pending_sends or []),
        delivery_contract=delivery_contract(t, wake_blocked=wake_blocked),
        ci_run_id=t.ci_run_id, effort=t.effort, labels=list(t.labels),
        timeline=stage_timeline(events, t.target, t.issue, now=now))


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
    """File exists as a request but cannot be read: missing, non-UTF-8, or containment violation."""
    kind: Literal["unavailable"] = "unavailable"
    path: str        # stored path (wt-relative or raw)
    reason: str      # "file-missing" | "not-utf8" | "path-escapes-worktree"


class OperatorRequest(BaseModel):
    kind: Literal["spec-approval", "answers"]
    content: Annotated[ReadableContent | UnavailableContent, Field(discriminator="kind")]


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


class ProviderUsageView(BaseModel):
    provider: str
    source: Source
    windows: list[WindowView]


class GateView(BaseModel):
    """The usage verdict for the policy default model: what an idle box would
    spawn next, and the verdict the dispatcher's stall/resume pings key on."""
    model: str
    provider: str
    admitted: bool
    note: str                  # verdict_note: the binding window and its numbers
    minutes_to_reset: float    # of the binding window; 0 when there is none
    binding: WindowView | None


class UsageView(BaseModel):
    providers: list[ProviderUsageView]
    gate: GateView


CLOSE_HEADROOM = 0.08


def _severity(headroom: float) -> Severity:
    return "blocked" if headroom <= 0 else "close" if headroom <= CLOSE_HEADROOM else "ok"


def _window_view(r: Reading, now: datetime) -> WindowView:
    return WindowView(kind=r.window.kind, scope=r.window.scope,
                      used=r.window.used, allowance=r.allowance,
                      headroom=r.headroom,
                      minutes_to_reset=minutes_to_reset(r.window, now),
                      severity=_severity(r.headroom))


def model_admission_view(usages: Mapping[str, ProviderUsage], *, now: datetime,
                         pace: PaceConfig, model: str) -> ModelAdmissionView:
    verdict = admits(usages, model, now, pace)
    return ModelAdmissionView(model=model, provider=verdict.provider,
                              admitted=verdict.admitted,
                              note=verdict_note(verdict, now))


def usage_view(usages: Mapping[str, ProviderUsage], *, now: datetime,
               pace: PaceConfig, default_model: str) -> UsageView:
    """Every provider's windows, sorted by provider name, and one gate: the
    verdict for the policy default model."""
    verdict = admits(usages, default_model, now, pace)
    binding = _window_view(verdict.binding, now) if verdict.binding else None
    return UsageView(
        providers=[ProviderUsageView(
            provider=name, source=usages[name].source,
            windows=[_window_view(r, now) for r in readings(usages[name], now, pace)])
            for name in sorted(usages)],
        gate=GateView(model=default_model, provider=verdict.provider,
                      admitted=verdict.admitted, note=verdict_note(verdict, now),
                      minutes_to_reset=binding.minutes_to_reset if binding else 0.0,
                      binding=binding))


class QuarantineEntry(BaseModel):
    target: str
    task_issue: int
    blocker_repo: str
    blocker_issue: int
    fingerprint: str
    created_at: str
    blocker_open: bool | None


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
    target: str
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


class TargetQueue(BaseModel):
    target: str
    as_of: str
    stale: bool
    rows: list[QueueRow]


class QueueView(BaseModel):
    targets: list[TargetQueue]


def target_queue(name: str, rows: list[dict], as_of: str, stale: bool,
                 in_flight: set[int]) -> TargetQueue:
    return TargetQueue(
        target=name, as_of=as_of, stale=stale,
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
    """(target, issue) -> ts of its FIRST claimed event. events.jsonl rotates
    at a size cap, so a task may have no claimed event at all — absent means
    unknown.

    Keyed on the pair, not the bare number: issue numbers are per-repo, so a
    merged alpha#73 from weeks ago would otherwise win the setdefault over a
    freshly claimed beta#73 and report a month-old claim time. Log lines
    written before events carried `target` land under ("", issue) and are read
    back as a fallback for any target — see claimed_at()."""
    out: dict[tuple[str, int], str] = {}
    for e in events:
        if e.get("event") == "claimed" and e.get("issue"):
            out.setdefault((str(e.get("target") or ""), e["issue"]),
                           e.get("ts", ""))
    return out


def claimed_at(index: dict[tuple[str, int], str], target: str,
               issue: int) -> str:
    """Exact (target, issue) match, else the untargeted legacy entry, else ""
    — never another target's claim."""
    return index.get((target, issue)) or index.get(("", issue), "")


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
                  claimed_at(claimed, str(e.get("target") or ""), e["issue"]),
                  e.get("ts", ""))) is not None]
    if not cycles:
        return None
    tail = sorted(cycles[-last:])
    n = len(tail)
    return tail[n // 2] if n % 2 else (tail[n // 2 - 1] + tail[n // 2]) / 2


_TIMELINE_EVENTS = {"claimed", "stage-started", "parked", "resumed", "merged"}


class NextClaimView(BaseModel):
    verdict: str          # will-claim|no-candidates|capacity-full|budget-blocked|claims-paused|unknown
    next_pass_eta: str    # ISO; "" when verdict == "unknown"
    next_issue: int = 0
    next_target: str = ""
    minutes_to_reset: float = 0
    blocked_by: str = ""


def _is_candidate(r: dict) -> bool:
    """Mirror of GitHubClient.candidates: what _claim_new would consume."""
    return (r.get("status") == "Ready" and not r.get("blocked")
            and "auto" in (r.get("labels") or []))


def _pass_eta(heartbeat: dict | None, now: datetime) -> str | None:
    """When the next dispatcher pass is due (ISO), or None when that is
    unknowable: no heartbeat, a malformed one, or one older than two
    intervals — the dispatcher may not be running."""
    hb = heartbeat or {}
    finished = _parse_ts(hb.get("finished_at", ""))
    try:
        interval = int(hb.get("interval_minutes") or 0)
        # A naive finished_at against an aware now raises TypeError: the zone
        # is unknown, so staleness cannot be judged either.
        fresh = (finished is not None and interval > 0
                 and (now - finished).total_seconds() <= 2 * interval * 60)
    except (ValueError, TypeError):
        return None
    return (finished + timedelta(minutes=interval)).isoformat() if fresh else None


def next_claim(heartbeat: dict | None, *, now: datetime,
               tasks: list[TaskState], capacity: int,
               gate: GateView,
               queues: list[tuple[str, list[dict]]],
               claims_paused: bool = False,
               triage_running: bool = False) -> NextClaimView:
    """A forecast of what _claim_new consumes next, from data already on the
    board request. It is a PARTIAL mirror, deliberately: the gates it models
    are the ones readable from local state (heartbeat, usage, capacity, the
    triage pause, the cached rank rows).

    The usage gate here is the policy default model's verdict, while the
    dispatcher gates each candidate on its own spec model: a candidate a rule
    routes to another model can be claimed while this says budget-blocked,
    or denied while this says will-claim.

    Gates it does NOT model, so `will-claim` can still be wrong:
      * failures.check_quarantine — a quarantined candidate is forecast as
        claimable. Resolving it needs a live `gh issue view` per record, which
        does not belong on a route polled once per second; the record is
        visible on /failures instead.
      * slot exhaustion (_claim_new breaks when allocate_slot returns None)
        and a create_workspace failure — only knowable at claim time.
      * queue movement since the rank rows were cached (rank_rows has a TTL,
        so the dispatcher's own pass may see a different head).
    """
    eta = _pass_eta(heartbeat, now)
    if eta is None:
        return NextClaimView(verdict="unknown", next_pass_eta="")
    # claims_paused wins over budget-blocked: when both are true claims are still
    # skipped, and the triage pause is the more actionable signal for the operator.
    if claims_paused:
        return NextClaimView(verdict="claims-paused", next_pass_eta=eta)
    if not gate.admitted:
        return NextClaimView(verdict="budget-blocked", next_pass_eta=eta,
                             minutes_to_reset=gate.minutes_to_reset,
                             blocked_by=gate.note)
    # Mirror the dispatcher pass: capacity is reduced by 1 while triage runs,
    # floored at 0 so a capacity=1 system does not claim during a triage sweep.
    eff_capacity = max(0, capacity - 1) if triage_running else capacity
    # Key on (target, issue) — same rationale as build_board: issue numbers
    # are per-repo so alpha#73 must not shadow beta#73 in the claim forecast.
    known = {(t.target, t.issue) for t in tasks}
    any_candidate = False
    for name, rows in queues:
        heads = [r for r in rows
                 if _is_candidate(r) and (name, r["number"]) not in known]
        if not heads:
            continue
        any_candidate = True
        mine = [t for t in tasks if t.target == name]
        if eff_capacity - len(active(mine)) > 0:
            return NextClaimView(verdict="will-claim", next_pass_eta=eta,
                                 next_issue=heads[0]["number"],
                                 next_target=name)
    return NextClaimView(
        verdict="capacity-full" if any_candidate else "no-candidates",
        next_pass_eta=eta)


def stage_timeline(events: list[dict], target: str, issue: int, *,
                   now: datetime) -> list[TimelineEntry]:
    """Closed segments between this issue's transition events, in order; the
    open tail (no merged yet) ends at `now` with ongoing=True. Sub-second
    segments (claimed -> immediate spec spawn) are dropped as noise.

    Keyed on (target, issue), not the bare number: issue numbers are
    per-repo, so two targets claiming the same issue number must not have
    their claimed/parked/resumed/merged events interleave into one garbled
    timeline. Log lines written before events carried `target` land with no
    target key and are read back for any target — same fallback convention
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
        if (e.get("issue") != issue
                or (e.get("target") or "") not in ("", target)
                or e.get("event") not in _TIMELINE_EVENTS):
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
