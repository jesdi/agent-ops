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
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    assert board.capacity.active == 1   # matches dispatcher.state.active()
    # #1 keeps its slot (a login park keeps its pane); #2 gave its back, and
    # slots_used counts held slots, not state files that still record one.
    assert board.capacity.slots_used == 1
    assert board.capacity.slots_held == [0]


def test_build_board_groups_and_counts():
    tasks = [
        make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
        make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN),
        make_task(issue=3, stage=Stage.PR_OPEN, slot=0),
    ]
    board = build_board(tasks, capacity=2,
                        models={1: "a", 2: "b", 3: "c"}, attached={1},
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    by_key = {c.key: c for c in board.columns}
    assert [c.issue for c in by_key["in-progress"].cards] == [1]
    assert [c.issue for c in by_key["parked"].cards] == [2]
    assert [c.issue for c in by_key["pr-open"].cards] == [3]
    assert by_key["in-progress"].cards[0].attached is True
    # capacity: parked releases capacity AND its slot; pr-open is not
    # in-flight at all. max_slots derives from capacity: capacity + 2 = 4.
    assert board.capacity.active == 1
    assert board.capacity.slots_used == 1   # only #1 still holds a slot
    assert board.capacity.capacity == 2
    assert board.capacity.max_slots == 4


def _scored_queue(*pairs):
    """(target, number, score) triples -> the queues shape build_board reads,
    grouped by target. score=None models a card no longer in the ranking."""
    by_target: dict[str, list[dict]] = {}
    for target, number, score in pairs:
        by_target.setdefault(target, []).append(
            {"number": number, "status": "Ready", "blocked": False,
             "labels": ["auto"], "title": f"t{number}",
             "url": f"https://x/{number}", "boost": 0, "score": score})
    return list(by_target.items())


def test_task_cards_carry_their_backlog_score():
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT)]
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=_scored_queue(("alpha", 1, 4.2)),
                        queue_stale=False, claims_paused=False,
                        triage_running=False)
    card = {c.key: c for c in board.columns}["in-progress"].cards[0]
    assert card.score == 4.2


def test_task_card_score_is_none_when_not_in_ranking():
    """A claimed task that dropped off the backlog board has no score."""
    tasks = [make_task(issue=5, stage=Stage.DONE)]
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    assert {c.key: c for c in board.columns}["done"].cards[0].score is None


def test_cards_sort_by_score_descending_nulls_last_within_a_column():
    tasks = [
        make_task(issue=1, stage=Stage.IMPLEMENT),   # score 2.0
        make_task(issue=2, stage=Stage.IMPLEMENT),   # score None
        make_task(issue=3, stage=Stage.IMPLEMENT),   # score 9.0
        make_task(issue=4, stage=Stage.IMPLEMENT),   # score None
    ]
    board = build_board(
        tasks, capacity=9, models={}, attached=set(), events=[],
        heartbeat=None, now=NOW, budget=BUDGET_OK,
        queues=_scored_queue(("alpha", 1, 2.0), ("alpha", 3, 9.0)),
        queue_stale=False, claims_paused=False, triage_running=False)
    cards = {c.key: c for c in board.columns}["in-progress"].cards
    # 3 (9.0) then 1 (2.0), then the two null-score cards in issue order.
    assert [c.issue for c in cards] == [3, 1, 2, 4]


def test_board_column_order_is_stable():
    board = build_board([], capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
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
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
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
    board = build_board(tasks, capacity=3, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    flagged = [c for col in board.columns for c in col.cards
               if c.consuming_capacity]
    assert len(flagged) == board.capacity.active
    assert sorted(c.issue for c in flagged) == [1, 2, 7]


from datetime import datetime

from web.read_model import (claimed_at, claimed_at_index, cycle_seconds,
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
    assert claimed_at_index(events) == {("alpha", 7): T0, ("alpha", 8): T1}


def test_claimed_at_index_separates_targets():
    """A merged alpha#73 from weeks ago must not win setdefault over a
    freshly claimed beta#73 — issue numbers are per-repo."""
    events = [ev(T0, "claimed", 73),
              {**ev(T2, "claimed", 73), "target": "beta"}]
    idx = claimed_at_index(events)
    assert claimed_at(idx, "alpha", 73) == T0
    assert claimed_at(idx, "beta", 73) == T2


def test_claimed_at_falls_back_to_untargeted_legacy_lines():
    """Log lines written before events carried `target` are still read for
    whatever target asks — the pre-fix behaviour, preserved."""
    idx = claimed_at_index([{**ev(T0, "claimed", 7), "target": ""}])
    assert claimed_at(idx, "alpha", 7) == T0
    assert claimed_at(idx, "beta", 7) == T0
    assert claimed_at({}, "alpha", 7) == ""


def test_median_cycle_attributes_durations_per_target():
    """alpha#73 claimed long ago + beta#73 claimed recently: beta's cycle must
    be measured from beta's own claim, not alpha's."""
    events = [ev("2026-07-01T00:00:00+00:00", "claimed", 73),  # alpha, old
              {**ev(T0, "claimed", 73), "target": "beta"},
              {**ev(T1, "merged", 73), "target": "beta"}]
    assert median_cycle_seconds(events) == 2400.0  # 40m, not 31 days


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


def test_stage_timeline_drops_segments_with_mixed_awareness():
    """A naive `ts` next to an aware `now` used to raise TypeError out of
    (end - start) and 500 /api/task/{issue}. The segment whose endpoints
    cannot be compared is DROPPED (never coerced into a guessed zone); the
    comparable segments around it survive."""
    events = [ev(T0, "claimed", 7),
              ev(T1, "stage-started", 7, "spec"),            # aware, 40m
              ev("2026-08-01T11:00:00", "parked", 7, "spec"),  # NAIVE
              ev(T3, "resumed", 7, "spec")]                  # aware
    tl = stage_timeline(events, 7, now=NOW)
    # spec (T0->T1 via claimed->stage-started is queued 40m), then the two
    # segments touching the naive timestamp vanish, then the open tail.
    labels = [(e.label, e.ongoing) for e in tl]
    assert ("parked", False) not in labels          # naive->aware dropped
    assert labels[0] == ("queued", False)           # aware->aware kept
    assert labels[-1] == ("spec", True)             # open tail still ongoing
    assert all(e.seconds >= 1 for e in tl)


def test_stage_timeline_all_naive_still_works():
    """Naive-only logs are internally consistent: durations are computable
    and must not be dropped just because `now` is aware elsewhere."""
    events = [{**ev("2026-08-01T10:00:00", "claimed", 7)},
              {**ev("2026-08-01T10:40:00", "stage-started", 7, "spec")}]
    tl = stage_timeline(events, 7,
                        now=datetime.fromisoformat("2026-08-01T11:00:00"))
    assert [(e.label, e.seconds) for e in tl] == [("queued", 2400.0),
                                                  ("spec", 1200.0)]


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


def test_build_board_merges_ghosts_next_claim_and_durations():
    tasks = [make_task(issue=7, stage=Stage.IMPLEMENT),
             make_task(issue=9, stage=Stage.DONE, slot=-1,
                       done_at="2026-08-01T12:00:00+00:00")]
    events = [ev(T0, "claimed", 7), ev(T0, "claimed", 9),
              ev("2026-08-01T12:00:00+00:00", "merged", 9)]
    board = build_board(
        tasks, capacity=2, models={7: "opus", 9: "opus"}, attached=set(),
        events=events, heartbeat=HB, now=NOW, budget=BUDGET_OK,
        queues=[("alpha", [row(7), row(73), row(74, blocked=True)])],
        queue_stale=False, claims_paused=False, triage_running=False)
    # ghosts: candidates only, minus in-flight; rank order preserved
    assert [g.number for g in board.upcoming] == [73]
    assert board.upcoming[0].target == "alpha"
    assert board.next_claim.verdict == "will-claim"
    assert board.next_claim.next_issue == 73
    assert board.median_cycle_seconds == 7200.0
    assert board.upcoming_stale is False
    cards = {c.issue: c for col in board.columns for c in col.cards}
    assert cards[7].claimed_at == T0 and cards[7].cycle_seconds is None
    assert cards[9].cycle_seconds == 7200.0


def test_build_board_degrades_without_events_or_heartbeat():
    board = build_board(
        [make_task(issue=7)], capacity=2, models={}, attached=set(),
        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
        queues=[("alpha", [])], queue_stale=True,
        claims_paused=False, triage_running=False)
    assert board.next_claim.verdict == "unknown"
    assert board.median_cycle_seconds is None
    assert board.upcoming == [] and board.upcoming_stale is True
    card = board.columns[1].cards[0]  # in-progress
    assert card.claimed_at == "" and card.cycle_seconds is None


# ---- cross-target issue-number collision tests ----------------------------

def test_build_board_cross_target_ghost_not_suppressed():
    """alpha#73 in-flight must NOT hide beta#73 ghost — issue numbers are
    per-repo. Old bare-number `known` would suppress the beta ghost; this
    test fails against that code and passes after the (target, issue) fix."""
    tasks = [make_task(issue=73, target="alpha", stage=Stage.IMPLEMENT)]
    queues = [
        ("alpha", [row(73)]),   # alpha#73 already in-flight — no ghost
        ("beta",  [row(73)]),   # beta#73 is a different issue — should ghost
    ]
    board = build_board(
        tasks, capacity=4, models={}, attached=set(),
        events=[], heartbeat=HB, now=NOW, budget=BUDGET_OK,
        queues=queues, queue_stale=False,
        claims_paused=False, triage_running=False)
    ghost_targets = [(g.number, g.target) for g in board.upcoming]
    assert (73, "beta") in ghost_targets, \
        "beta#73 ghost missing — known set wrongly keyed on bare issue number"
    assert (73, "alpha") not in ghost_targets, \
        "alpha#73 ghost present — in-flight task not excluded on same target"


def test_build_board_same_target_still_excluded():
    """alpha#73 in-flight must still NOT appear as an alpha ghost (regression
    guard: the fix must not accidentally stop excluding same-target issues)."""
    tasks = [make_task(issue=73, target="alpha", stage=Stage.IMPLEMENT)]
    board = build_board(
        tasks, capacity=4, models={}, attached=set(),
        events=[], heartbeat=HB, now=NOW, budget=BUDGET_OK,
        queues=[("alpha", [row(73), row(74)])], queue_stale=False,
        claims_paused=False, triage_running=False)
    assert [g.number for g in board.upcoming] == [74]


def test_next_claim_cross_target_not_suppressed():
    """alpha#73 in-flight must NOT prevent beta#73 from being the will-claim
    head. Old bare-number `known` skips beta#73; this test fails against that
    code and passes after the (target, issue) fix."""
    tasks = [make_task(issue=73, target="alpha", stage=Stage.IMPLEMENT)]
    queues = [
        ("alpha", []),        # alpha has no additional candidates
        ("beta",  [row(73)]), # beta#73 should be claimable
    ]
    v = next_claim(HB, now=NOW, tasks=tasks, capacity=4, budget=BUDGET_OK,
                   queues=queues, claims_paused=False, triage_running=False)
    assert v.verdict == "will-claim"
    assert v.next_target == "beta" and v.next_issue == 73


def test_next_claim_same_target_still_skipped():
    """alpha#73 in-flight must still be skipped on alpha's own queue."""
    tasks = [make_task(issue=73, target="alpha", stage=Stage.IMPLEMENT)]
    queues = [("alpha", [row(73), row(74)])]
    v = next_claim(HB, now=NOW, tasks=tasks, capacity=4, budget=BUDGET_OK,
                   queues=queues, claims_paused=False, triage_running=False)
    assert v.verdict == "will-claim" and v.next_issue == 74


# ---------------------------------------------------------------------------
# Task 6: message thread, delivery contract, undelivered badge
# ---------------------------------------------------------------------------

from dispatcher import messages as msgq
from web.read_model import (delivery_contract, message_views, task_detail)


def _msg(mid, text, delivered=""):
    return msgq.Message(id=mid, text=text, actor="jesdi@github",
                        created_at="2026-08-12T10:00:00+00:00",
                        delivered_at=delivered)


def test_message_states_are_derived_from_the_two_sources():
    views = message_views(
        [_msg("a", "delivered one", "2026-08-12T10:05:00+00:00"),
         _msg("b", "queued one")],
        [{"id": "intent-1", "text": "just sent", "actor": "jesdi@github",
          "created_at": "2026-08-12T10:06:00+00:00"}])
    assert [(v.text, v.state) for v in views] == [
        ("delivered one", "delivered"),
        ("queued one", "queued"),
        ("just sent", "sending")]


def test_sending_entries_come_last_even_with_odd_timestamps():
    views = message_views(
        [_msg("a", "queued one")],
        [{"id": "i", "text": "sent", "actor": "op", "created_at": ""}])
    assert [v.state for v in views] == ["queued", "sending"]


def test_delivery_contract_unclaimed_issue():
    assert delivery_contract(None, wake_blocked=False) == (
        "will deliver when this task is claimed")


@pytest.mark.parametrize("stage", [Stage.DONE, Stage.FAILED])
def test_delivery_contract_finished_task(stage):
    t = make_task(stage=stage)
    assert delivery_contract(t, wake_blocked=False) == (
        "will deliver if this task restarts")


def test_delivery_contract_parked_and_starved():
    t = make_task(park=PARK_HUMAN)
    assert delivery_contract(t, wake_blocked=True) == (
        "will deliver when the session resumes — waiting for a free slot")
    assert delivery_contract(t, wake_blocked=False) == (
        "will deliver when the session resumes")


def test_delivery_contract_running_session():
    t = make_task(park="")
    assert delivery_contract(t, wake_blocked=False) == (
        "will deliver at the next session boundary — this session is still "
        "running")


def test_task_detail_carries_thread_and_contract():
    t = make_task(issue=7, park=PARK_HUMAN)
    detail = task_detail(t, model="m", attached=False, pane_tail="",
                         session_alive=False, events=[],
                         now=NOW,
                         messages=[_msg("a", "hi")], pending_sends=[],
                         wake_blocked=False)
    assert [m.state for m in detail.messages] == ["queued"]
    assert detail.delivery_contract == (
        "will deliver when the session resumes")


def test_card_carries_undelivered_count_and_blocked_flag():
    tasks = [make_task(issue=7)]
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None,
                        now=NOW,
                        budget=BUDGET_OK, queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False,
                        undelivered={7: 3}, wake_blocked={7})
    card = [c for col in board.columns for c in col.cards][0]
    assert card.undelivered_messages == 3
    assert card.wake_blocked is True


def test_capacity_view_reports_held_slots_and_derived_max():
    tasks = [make_task(issue=7, slot=0), make_task(issue=8, slot=2)]
    board = build_board(tasks, capacity=3, models={}, attached=set(),
                        events=[], heartbeat=None,
                        now=NOW,
                        budget=BUDGET_OK, queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    assert board.capacity.slots_held == [0, 2]
    assert board.capacity.max_slots == 5


def test_a_pending_intent_with_no_text_is_not_a_message():
    """The Resume button posts `{}`, so a resume intent yields text="" — an
    empty "sending" bubble in the thread. Nothing was typed; nothing renders."""
    views = message_views([_msg("a", "queued one")],
                          [{"id": "i", "text": "", "actor": "op",
                            "created_at": "2026-08-12T10:06:00+00:00"},
                           {"id": "j", "text": "   ", "actor": "op",
                            "created_at": "2026-08-12T10:07:00+00:00"},
                           {"id": "k", "text": "ship it", "actor": "op",
                            "created_at": "2026-08-12T10:08:00+00:00"}])
    assert [(v.text, v.state) for v in views] == [
        ("queued one", "queued"), ("ship it", "sending")]


def test_slots_used_never_disagrees_with_the_lit_segments():
    """The gauge prints slots_used and lights slots_held: on old on-disk state
    (a parked task still recording a slot) counting `slot != NO_SLOT` read
    "1/4" with zero segments lit. One derivation, one truth."""
    tasks = [make_task(issue=1, stage=Stage.IMPLEMENT, slot=0),
             make_task(issue=2, stage=Stage.SPEC, slot=1, park=PARK_HUMAN)]
    board = build_board(tasks, capacity=2, models={}, attached=set(),
                        events=[], heartbeat=None, now=NOW, budget=BUDGET_OK,
                        queues=[], queue_stale=False,
                        claims_paused=False, triage_running=False)
    assert board.capacity.slots_held == [0]
    assert board.capacity.slots_used == len(board.capacity.slots_held)
