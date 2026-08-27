"""Pure view models: TaskState + rank rows + budget + failures + events ->
Pydantic responses. NO I/O in this module — construction only."""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from dispatcher import messages as msgq
from dispatcher.budget import UsageSnapshot, should_spawn
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
    attached: bool
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


class CapacityView(BaseModel):
    active: int
    capacity: int
    slots_used: int
    max_slots: int
    slots_held: list[int] = []


class BoardView(BaseModel):
    columns: list[Column]
    capacity: CapacityView
    upcoming: list[GhostCard]
    upcoming_stale: bool
    next_claim: NextClaimView
    median_cycle_seconds: float | None


def task_card(t: TaskState, *, model: str, attached: bool,
              claimed_at: str = "",
              cycle_seconds: float | None = None,
              score: float | None = None,
              undelivered_messages: int = 0,
              wake_blocked: bool = False) -> TaskCard:
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
        updated_at=t.updated_at, attached=attached,
        consuming_capacity=consumes_capacity(t),
        claimed_at=claimed_at, cycle_seconds=cycle_seconds, score=score,
        undelivered_messages=undelivered_messages,
        wake_blocked=wake_blocked)


def build_board(tasks: list[TaskState], *, capacity: int,
                models: dict[tuple[str, int], str],
                attached: set[tuple[str, int]],
                events: list[dict], heartbeat: dict | None, now: datetime,
                budget: BudgetView, queues: list[tuple[str, list[dict]]],
                queue_stale: bool, claims_paused: bool,
                triage_running: bool,
                undelivered: dict[tuple[str, int], int] | None = None,
                wake_blocked: set[tuple[str, int]] | None = None
                ) -> BoardView:
    mail = undelivered or {}
    blocked = wake_blocked or set()
    claimed = claimed_at_index(events)
    # (target, number) -> score from the rank rows already on the request, so
    # a task card can show the same backlog score its ghost card would.
    scores = {(name, r["number"]): r.get("score")
              for name, rows in queues for r in rows}
    cards = []
    for t in tasks:
        key = (t.target, t.issue)
        at = claimed_at(claimed, t.target, t.issue)
        cards.append(task_card(t, model=models.get(key, ""),
                               attached=key in attached,
                               claimed_at=at,
                               cycle_seconds=cycle_seconds(at, t.done_at),
                               score=scores.get(key),
                               undelivered_messages=mail.get(key, 0),
                               wake_blocked=key in blocked))
    by_column: dict[str, list[TaskCard]] = {key: [] for key, _ in COLUMNS}
    for card in cards:
        by_column[card.column].append(card)
    # Highest score at the top of every column; unscored cards fall to the
    # bottom, issue number as a stable tiebreaker.
    for col_cards in by_column.values():
        col_cards.sort(key=lambda c: (c.score is None, -(c.score or 0.0),
                                      c.issue))
    in_flight = [t for t in tasks if t.stage in IN_FLIGHT_STAGES]
    # Which numbers, not just how many: the console colours a card by its
    # slot, and the gauge must light the same segments. slots_used is the
    # LENGTH of that list, never a second count — counting `slot != NO_SLOT`
    # instead made old on-disk state (a parked task still recording a slot)
    # read "1/4" with zero segments lit.
    slots_held = sorted({t.slot for t in in_flight
                         if holds_slot(t) and t.slot != NO_SLOT})
    # Key on (target, issue) so alpha#73 does not hide beta#73. Issue numbers
    # are per-repo; bare numbers would wrongly suppress cross-target candidates
    # (cf. dispatcher/main.py:223 which acknowledges number collisions).
    known = {(t.target, t.issue) for t in tasks}
    upcoming = [GhostCard(number=r["number"], target=name,
                          title=r.get("title", ""), url=r.get("url", ""),
                          score=r.get("score"), boost=int(r.get("boost") or 0))
                for name, rows in queues for r in rows
                if _is_candidate(r) and (name, r["number"]) not in known]
    return BoardView(
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
        upcoming=upcoming, upcoming_stale=queue_stale,
        next_claim=next_claim(heartbeat, now=now, tasks=tasks,
                              capacity=capacity, budget=budget,
                              queues=queues,
                              claims_paused=claims_paused,
                              triage_running=triage_running),
        median_cycle_seconds=median_cycle_seconds(events))


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


def task_detail(t: TaskState, *, model: str, attached: bool,
                pane_tail: str, session_alive: bool,
                events: list[dict], now: datetime,
                messages: list[msgq.Message] | None = None,
                pending_sends: list[dict] | None = None,
                wake_blocked: bool = False) -> TaskDetail:
    at = claimed_at(claimed_at_index(events), t.target, t.issue)
    msgs = messages or []
    return TaskDetail(
        card=task_card(t, model=model, attached=attached,
                       claimed_at=at,
                       cycle_seconds=cycle_seconds(at, t.done_at),
                       undelivered_messages=len(
                           [m for m in msgs if not m.delivered_at]),
                       wake_blocked=wake_blocked),
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


class SpecView(BaseModel):
    path: str      # worktree-relative
    markdown: str


class PaneHistory(BaseModel):
    text: str


class BudgetView(BaseModel):
    utilization: float
    minutes_to_reset: float
    source: str
    would_spawn: bool
    threshold_applied: str


def budget_view(u: UsageSnapshot, threshold: float, racing_minutes: int,
                racing_threshold: float) -> BudgetView:
    if u.source == "unavailable":
        applied = "n/a"
    elif u.minutes_to_reset <= racing_minutes:
        applied = "reset-racing"
    else:
        applied = "base"
    return BudgetView(
        utilization=u.utilization, minutes_to_reset=u.minutes_to_reset,
        source=u.source,
        would_spawn=should_spawn(u, threshold, racing_minutes,
                                 racing_threshold),
        threshold_applied=applied)


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


def _is_candidate(r: dict) -> bool:
    """Mirror of GitHubClient.candidates: what _claim_new would consume."""
    return (r.get("status") == "Ready" and not r.get("blocked")
            and "auto" in (r.get("labels") or []))


def next_claim(heartbeat: dict | None, *, now: datetime,
               tasks: list[TaskState], capacity: int, budget: BudgetView,
               queues: list[tuple[str, list[dict]]],
               claims_paused: bool = False,
               triage_running: bool = False) -> NextClaimView:
    """A forecast of what _claim_new consumes next, from data already on the
    board request. It is a PARTIAL mirror, deliberately: the gates it models
    are the ones readable from local state (heartbeat, budget, capacity, the
    triage pause, the cached rank rows).

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
    hb = heartbeat or {}
    finished = _parse_ts(hb.get("finished_at", ""))
    try:
        interval = int(hb.get("interval_minutes") or 0)
    except (ValueError, TypeError):
        return NextClaimView(verdict="unknown", next_pass_eta="")
    if finished is None or interval <= 0:
        return NextClaimView(verdict="unknown", next_pass_eta="")
    try:
        stale = (now - finished).total_seconds() > 2 * interval * 60
    except TypeError:
        # Mixed-awareness comparison (one naive, one aware): zone is unknown,
        # so we cannot determine staleness — treat as unknown.
        return NextClaimView(verdict="unknown", next_pass_eta="")
    if stale:
        return NextClaimView(verdict="unknown", next_pass_eta="")
    eta = (finished + timedelta(minutes=interval)).isoformat()
    # Mirror dispatcher line 1156: `if budget_ok and not claims_paused: _claim_new(...)`.
    # claims_paused wins over budget-blocked: when both are true claims are still
    # skipped, and the triage pause is the more actionable signal for the operator.
    if claims_paused:
        return NextClaimView(verdict="claims-paused", next_pass_eta=eta)
    if not budget.would_spawn:
        return NextClaimView(verdict="budget-blocked", next_pass_eta=eta,
                             minutes_to_reset=budget.minutes_to_reset)
    # Mirror dispatcher line 1136: capacity is reduced by 1 when triage is running,
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
