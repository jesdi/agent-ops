"""Pure view-model tests: every (stage, park) combination, board grouping."""
import pytest

from dispatcher.state import (NO_SLOT, PARK_CI, PARK_HUMAN, PARK_LOGIN,
                              PARK_REVIEW, PARK_WAKE, Stage)
from tests.webfakes import make_task
from web.read_model import COLUMNS, build_board, column_for, task_card

STAGE_COLUMNS = {
    Stage.QUEUED: "queued",
    Stage.SPEC: "in-progress",
    Stage.PLAN: "in-progress",
    Stage.IMPLEMENT: "in-progress",
    Stage.AWAITING_SPEC_REVIEW: "needs-review",
    Stage.PR_OPEN: "pr-open",
    Stage.ADDRESS_REVIEW: "in-progress",
    Stage.DONE: "done",
    Stage.FAILED: "failed",
    Stage.BLOCKED: "failed",         # legacy stage: surfaced, not hidden
    Stage.STALLED_ON_BUDGET: "stalled",
}


@pytest.mark.parametrize("stage", list(Stage))
def test_park_overrides_stage(stage):
    assert column_for(stage.value, PARK_HUMAN) == "parked"
    assert column_for(stage.value, PARK_CI) == "awaiting-ci"
    assert column_for(stage.value, PARK_WAKE) == "resuming"
    # a login park is the state that most needs an operator — never let it
    # render as healthy in-progress work
    assert column_for(stage.value, PARK_LOGIN) == "parked"
    assert column_for(stage.value, PARK_REVIEW) == "needs-review"


@pytest.mark.parametrize("stage", list(Stage))
def test_unparked_column_per_stage(stage):
    assert column_for(stage.value, "") == STAGE_COLUMNS[stage]


def test_task_card_fields():
    t = make_task(issue=7, stage=Stage.SPEC, slot=1, park=PARK_HUMAN,
                  park_msg_id=0)
    card = task_card(t, model="claude-sonnet-4-5", attached=True)
    assert card.issue == 7
    assert card.stage == "spec"
    assert card.park == "parked"
    assert card.column == "parked"
    assert card.model == "claude-sonnet-4-5"
    assert card.attached is True
    # parked-for-human with no Telegram message id yet -> note pending
    assert card.park_note_pending is True


def test_park_note_not_pending_once_notified():
    t = make_task(park=PARK_HUMAN, park_msg_id=123)
    assert task_card(t, model="m", attached=False).park_note_pending is False


def test_login_park_card_shows_its_park_kind_in_the_parked_column():
    t = make_task(issue=9, stage=Stage.IMPLEMENT, park=PARK_LOGIN,
                  park_msg_id=77)
    card = task_card(t, model="m", attached=False)
    assert card.column == "parked"
    assert card.park == PARK_LOGIN  # the card still distinguishes the kind
    # a login park always has a Telegram message to reply to (a failed send
    # degrades to a plain park), so it is never a "note pending" card
    assert card.park_note_pending is False


def test_login_park_counts_towards_active_capacity():
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT, slot=0, park=PARK_LOGIN),
             make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN)]
    board = build_board(tasks, capacity=2, models={}, attached=set())
    assert board.capacity.active == 1   # matches dispatcher.state.active()
    assert board.capacity.slots_used == 2


def test_build_board_groups_and_counts():
    tasks = [
        make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
        make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN),
        make_task(issue=3, stage=Stage.PR_OPEN, slot=0),
    ]
    board = build_board(tasks, capacity=2,
                        models={1: "a", 2: "b", 3: "c"}, attached={1})
    by_key = {c.key: c for c in board.columns}
    assert [c.issue for c in by_key["in-progress"].cards] == [1]
    assert [c.issue for c in by_key["parked"].cards] == [2]
    assert [c.issue for c in by_key["pr-open"].cards] == [3]
    assert by_key["in-progress"].cards[0].attached is True
    # capacity: parked releases capacity but keeps its slot;
    # pr-open is not in-flight at all.
    assert board.capacity.active == 1
    assert board.capacity.slots_used == 2
    assert board.capacity.capacity == 2
    assert board.capacity.max_slots == 3


def test_board_column_order_is_stable():
    board = build_board([], capacity=2, models={}, attached=set())
    assert [c.key for c in board.columns] == [
        "queued", "in-progress", "needs-review", "pr-open", "done", "parked",
        "awaiting-ci", "resuming", "stalled", "failed"]


def test_new_stage_columns():
    assert column_for("address-review", "") == "in-progress"
    assert column_for("done", "") == "done"


def test_done_column_present_after_pr_review():
    keys = [k for k, _ in COLUMNS]
    assert keys.index("done") == keys.index("pr-open") + 1
    titles = dict(COLUMNS)
    assert titles["pr-open"] == "PR review" and titles["done"] == "Done"


def test_task_card_carries_feedback_pending():
    t = make_task(stage=Stage.PR_OPEN, feedback_pending=True)
    assert task_card(t, model="", attached=False).feedback_pending is True


def test_review_park_shows_in_the_needs_review_column():
    t = make_task(issue=9, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT,
                  park=PARK_REVIEW, park_msg_id=77)
    card = task_card(t, model="m", attached=False)
    assert card.column == "needs-review"
    assert card.park == PARK_REVIEW   # the card still distinguishes the kind
    assert card.park_note_pending is False


def test_gate_parked_tasks_hold_neither_capacity_nor_a_slot():
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
             make_task(issue=2, stage=Stage.AWAITING_SPEC_REVIEW,
                       slot=NO_SLOT, park=PARK_REVIEW)]
    board = build_board(tasks, capacity=2, models={}, attached=set())
    assert board.capacity.active == 1
    assert board.capacity.slots_used == 1   # not 2 — #2 gave its slot back


def test_task_card_carries_park_note():
    t = make_task(park="parked", park_note="which URL?")
    card = task_card(t, model="opus", attached=False)
    assert card.park_note == "which URL?"


def test_task_card_flags_an_unparked_in_flight_task():
    card = task_card(make_task(stage=Stage.IMPLEMENT), model="opus",
                     attached=False)
    assert card.consuming_capacity is True


def test_task_card_does_not_flag_a_ci_parked_task():
    card = task_card(make_task(stage=Stage.IMPLEMENT, park=PARK_CI),
                     model="opus", attached=False)
    assert card.consuming_capacity is False


def test_task_card_flags_a_login_parked_task():
    """The card the operator most needs to spot: parked, yet still holding a
    unit, which is why the dispatcher has stopped claiming."""
    card = task_card(make_task(stage=Stage.SPEC, park=PARK_LOGIN),
                     model="opus", attached=False)
    assert card.consuming_capacity is True


def test_flagged_cards_reconcile_with_the_capacity_count():
    """What the operator does by eye: count the accented cards, compare to the
    header. The two must never disagree."""
    tasks = [
        make_task(issue=1, stage=Stage.IMPLEMENT),
        make_task(issue=2, stage=Stage.SPEC, park=PARK_LOGIN),
        make_task(issue=3, stage=Stage.IMPLEMENT, park=PARK_CI),
        make_task(issue=4, stage=Stage.AWAITING_SPEC_REVIEW, park=PARK_REVIEW),
        make_task(issue=5, stage=Stage.DONE),
        make_task(issue=6, stage=Stage.FAILED),
        make_task(issue=7, stage=Stage.QUEUED),
    ]
    board = build_board(tasks, capacity=3, models={}, attached=set())
    flagged = [c for col in board.columns for c in col.cards
               if c.consuming_capacity]
    assert len(flagged) == board.capacity.active
    assert sorted(c.issue for c in flagged) == [1, 2, 7]


from datetime import datetime

from web.read_model import (claimed_at_index, cycle_seconds,
                            median_cycle_seconds, stage_timeline)


def ev(ts, event, issue, stage="", detail=""):
    return {"ts": ts, "event": event, "target": "alpha", "issue": issue,
            "stage": stage, "model": "", "actor": "dispatcher",
            "detail": detail}


T0 = "2026-08-01T10:00:00+00:00"
T1 = "2026-08-01T10:40:00+00:00"   # +40m
T2 = "2026-08-01T11:00:00+00:00"   # +1h
T3 = "2026-08-01T12:00:00+00:00"   # +2h
NOW = datetime.fromisoformat("2026-08-01T12:30:00+00:00")


def test_claimed_at_index_first_wins_and_ignores_other_events():
    events = [ev(T0, "claimed", 7), ev(T1, "stage-started", 7, "spec"),
              ev(T2, "claimed", 7), ev(T1, "claimed", 8)]
    assert claimed_at_index(events) == {7: T0, 8: T1}


def test_cycle_seconds_valid_invalid_negative():
    assert cycle_seconds(T0, T3) == 7200.0
    assert cycle_seconds("", T3) is None
    assert cycle_seconds(T0, "not-a-date") is None
    assert cycle_seconds(T3, T0) is None  # clock skew: no negative durations


def test_cycle_seconds_mixed_awareness_returns_none():
    # naive vs aware: zone unknown, so duration is unknowable — must not raise
    assert cycle_seconds("2026-08-01T10:00:00", "2026-08-01T12:00:00+00:00") is None
    assert cycle_seconds("2026-08-01T12:00:00+00:00", "2026-08-01T10:00:00") is None


def test_median_cycle_window_uses_last_20_not_all():
    # Build 22 completed pairs: issues 1-22, durations 1h..22h.
    # Last 20 are issues 3-22 (durations 3h..22h, i.e. 10800..79200 seconds).
    # Median of last 20 (sorted): indices 9 and 10 → (12*3600 + 13*3600) / 2
    # = (43200 + 46800) / 2 = 45000.0.
    # Median of all 22 would be (11*3600 + 12*3600) / 2 = 41400.0 — different.
    events = []
    for i in range(1, 23):
        claim_ts = "2026-08-01T00:00:00+00:00"
        merge_ts = f"2026-08-01T{i:02d}:00:00+00:00"
        events.append(ev(claim_ts, "claimed", i))
        events.append(ev(merge_ts, "merged", i))
    result = median_cycle_seconds(events, last=20)
    assert result == (12 * 3600 + 13 * 3600) / 2  # 45000.0


def test_median_cycle_over_merges_skipping_rotated_claims():
    events = [ev(T0, "claimed", 1), ev(T1, "merged", 1),      # 40m
              ev(T0, "claimed", 2), ev(T3, "merged", 2),      # 2h
              ev(T2, "merged", 3)]                             # claim rotated away
    assert median_cycle_seconds(events) == (2400.0 + 7200.0) / 2


def test_median_none_when_no_complete_pairs():
    assert median_cycle_seconds([ev(T0, "claimed", 1)]) is None
    assert median_cycle_seconds([]) is None


def test_stage_timeline_stages_parks_and_merge():
    events = [ev(T0, "claimed", 7),
              ev(T0, "stage-started", 7, "spec"),   # 0s queued segment dropped
              ev(T1, "parked", 7, "spec"),          # spec 40m
              ev(T2, "resumed", 7, "spec"),         # parked 20m
              ev(T3, "merged", 7)]                  # spec (resumed) 1h
    tl = stage_timeline(events, 7, now=NOW)
    assert [(e.label, e.seconds, e.kind, e.ongoing) for e in tl] == [
        ("spec", 2400.0, "stage", False),
        ("parked", 1200.0, "parked", False),
        ("spec", 3600.0, "stage", False)]


def test_stage_timeline_open_segment_is_ongoing_and_other_issues_ignored():
    events = [ev(T0, "claimed", 7), ev(T0, "stage-started", 7, "implement"),
              ev(T1, "stage-started", 9, "spec")]
    tl = stage_timeline(events, 7, now=NOW)
    assert [(e.label, e.kind, e.ongoing) for e in tl] == [
        ("implement", "stage", True)]
    assert tl[0].seconds == 9000.0  # T0 -> NOW


def test_stage_timeline_empty_for_unknown_or_rotated_issue():
    assert stage_timeline([], 7, now=NOW) == []


from web.read_model import BudgetView, next_claim

HB = {"started_at": "2026-08-01T12:25:00+00:00",
      "finished_at": "2026-08-01T12:26:00+00:00", "interval_minutes": 10}
BUDGET_OK = BudgetView(utilization=0.5, minutes_to_reset=120, source="oauth",
                       would_spawn=True, threshold_applied="base")
BUDGET_NO = BudgetView(utilization=0.95, minutes_to_reset=130, source="oauth",
                       would_spawn=False, threshold_applied="base")


def row(number, status="Ready", blocked=False, labels=("auto",)):
    return {"number": number, "status": status, "blocked": blocked,
            "labels": list(labels), "title": f"t{number}",
            "url": f"https://x/{number}", "boost": 0, "score": 1.0}


def test_next_claim_unknown_when_heartbeat_missing_or_stale():
    v = next_claim(None, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                   queues=[("alpha", [row(1)])])
    assert (v.verdict, v.next_pass_eta) == ("unknown", "")
    stale = dict(HB, finished_at="2026-08-01T12:09:59+00:00")  # >2x10m before NOW
    assert next_claim(stale, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                      queues=[]).verdict == "unknown"
    # exactly at the boundary (20m old) is still fresh
    edge = dict(HB, finished_at="2026-08-01T12:10:00+00:00")
    assert next_claim(edge, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                      queues=[]).verdict == "no-candidates"
    assert next_claim({"garbage": True}, now=NOW, tasks=[], capacity=2,
                      budget=BUDGET_OK, queues=[]).verdict == "unknown"


def test_next_claim_will_claim_head_of_queue():
    v = next_claim(HB, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                   queues=[("alpha", [row(70, status="In progress"),
                                      row(71, blocked=True),
                                      row(72, labels=()),
                                      row(73)])])
    assert v.verdict == "will-claim"
    assert (v.next_issue, v.next_target) == (73, "alpha")
    assert v.next_pass_eta == "2026-08-01T12:36:00+00:00"  # finished + 10m


def test_next_claim_skips_already_claimed_issues():
    v = next_claim(HB, now=NOW, tasks=[make_task(issue=73)], capacity=2,
                   budget=BUDGET_OK, queues=[("alpha", [row(73), row(74)])])
    assert (v.verdict, v.next_issue) == ("will-claim", 74)


def test_next_claim_budget_blocked_beats_everything_else():
    v = next_claim(HB, now=NOW, tasks=[], capacity=2, budget=BUDGET_NO,
                   queues=[("alpha", [row(73)])])
    assert (v.verdict, v.minutes_to_reset) == ("budget-blocked", 130)
    assert v.next_pass_eta == "2026-08-01T12:36:00+00:00"  # ETA set on non-unknown verdicts


def test_next_claim_capacity_full_and_no_candidates():
    busy = [make_task(issue=i) for i in (1, 2)]  # IMPLEMENT: active, unparked
    assert next_claim(HB, now=NOW, tasks=busy, capacity=2, budget=BUDGET_OK,
                      queues=[("alpha", [row(73)])]).verdict == "capacity-full"
    assert next_claim(HB, now=NOW, tasks=busy, capacity=2, budget=BUDGET_OK,
                      queues=[("alpha", [])]).verdict == "no-candidates"


def test_next_claim_unknown_on_bad_interval():
    assert next_claim(dict(HB, interval_minutes="ten"), now=NOW, tasks=[],
                      capacity=2, budget=BUDGET_OK, queues=[]).verdict == "unknown"
    assert next_claim(dict(HB, interval_minutes=[]), now=NOW, tasks=[],
                      capacity=2, budget=BUDGET_OK, queues=[]).verdict == "unknown"
    # naive finished_at with aware now raises TypeError on subtraction
    naive_hb = dict(HB, finished_at="2026-08-01T12:26:00")
    assert next_claim(naive_hb, now=NOW, tasks=[],
                      capacity=2, budget=BUDGET_OK, queues=[]).verdict == "unknown"


def test_next_claim_per_target_capacity_filter():
    # alpha is capacity-full (2 active tasks, capacity=2) but has a candidate;
    # beta has one active task and one slot free with a candidate.
    # Collapsing `mine = tasks` would wrongly count 3 active units for beta and
    # produce capacity-full instead of will-claim.
    alpha_tasks = [make_task(issue=1, target="alpha"),
                   make_task(issue=2, target="alpha")]
    beta_tasks = [make_task(issue=3, target="beta")]
    v = next_claim(HB, now=NOW, tasks=alpha_tasks + beta_tasks, capacity=2,
                   budget=BUDGET_OK,
                   queues=[("alpha", [row(10)]), ("beta", [row(20)])])
    assert v.verdict == "will-claim"
    assert v.next_target == "beta"
    assert v.next_issue == 20


def test_next_claim_claims_paused():
    # claims-paused fires even when the queue has a will-claim candidate
    v = next_claim(HB, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                   queues=[("alpha", [row(73)])], claims_paused=True)
    assert v.verdict == "claims-paused"
    assert v.next_pass_eta != ""
    # claims-paused wins over budget-blocked: both conditions skip claiming,
    # but triage pause is the more actionable operator signal
    v2 = next_claim(HB, now=NOW, tasks=[], capacity=2, budget=BUDGET_NO,
                    queues=[("alpha", [row(73)])], claims_paused=True)
    assert v2.verdict == "claims-paused"


def test_next_claim_triage_running_reduces_effective_capacity():
    # Without triage: capacity=1, no active tasks → will-claim normally
    assert next_claim(HB, now=NOW, tasks=[], capacity=1, budget=BUDGET_OK,
                      queues=[("alpha", [row(10)])]).verdict == "will-claim"
    # With triage: capacity=1, no active tasks → effective=0 → capacity-full
    v = next_claim(HB, now=NOW, tasks=[], capacity=1, budget=BUDGET_OK,
                   queues=[("alpha", [row(10)])], triage_running=True)
    assert v.verdict == "capacity-full"
    # With triage: capacity=2, no active tasks → effective=1 > 0 → still claims
    v2 = next_claim(HB, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                    queues=[("alpha", [row(10)])], triage_running=True)
    assert v2.verdict == "will-claim"


def test_next_claim_unknown_wins_over_new_gates():
    # unknown (missing heartbeat) overrides both new gates
    assert next_claim(None, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                      queues=[("alpha", [row(1)])],
                      claims_paused=True, triage_running=True).verdict == "unknown"
    # unknown (stale heartbeat) also overrides
    stale = dict(HB, finished_at="2026-08-01T12:09:59+00:00")
    assert next_claim(stale, now=NOW, tasks=[], capacity=2, budget=BUDGET_OK,
                      queues=[("alpha", [row(1)])],
                      claims_paused=True, triage_running=True).verdict == "unknown"
