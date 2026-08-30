import json
from dataclasses import replace
from pathlib import Path

import pytest

from dispatcher.state import (
    IN_FLIGHT_STAGES,
    NO_SLOT,
    PARK_CI,
    PARK_HUMAN,
    PARK_LOGIN,
    PARK_REVIEW,
    PARK_WAKE,
    Stage,
    StageSignal,
    TaskState,
    active,
    allocate_slot,
    consumes_capacity,
    delete,
    load,
    load_all,
    read_stage_signal,
    save,
)


def make(issue=101, stage=Stage.SPEC, slot=0, **kw):
    return TaskState(
        issue=issue,
        target="portfolio_eval",
        stage=stage,
        slot=slot,
        worktree=f"/home/agent/repos/portfolio_eval.worktrees/task-{issue}",
        branch=f"agent/task-{issue}",
        title="Add widget",
        updated_at="2026-07-14T12:00:00+00:00",
        **kw,
    )


def test_save_load_roundtrip(tmp_path: Path):
    ts = make()
    save(tmp_path, ts)
    assert (tmp_path / "task-portfolio_eval-101.json").exists()
    loaded = load(tmp_path, "portfolio_eval", 101)
    assert loaded == ts
    assert loaded.stage is Stage.SPEC


def test_load_missing_returns_none(tmp_path: Path):
    assert load(tmp_path, "portfolio_eval", 999) is None


def test_load_all_sorted_by_issue(tmp_path: Path):
    save(tmp_path, make(issue=202, slot=1))
    save(tmp_path, make(issue=101, slot=0))
    assert [t.issue for t in load_all(tmp_path)] == [101, 202]


def test_allocate_slot_picks_first_free():
    existing = [make(issue=1, slot=0), make(issue=2, slot=2)]
    assert allocate_slot(existing, max_slots=3) == 1


def test_allocate_slot_full_returns_none():
    existing = [make(issue=i, slot=i) for i in range(3)]
    assert allocate_slot(existing, max_slots=3) is None


def test_terminal_stages_do_not_hold_slots():
    # a FAILED task's slot is reusable
    existing = [make(issue=1, slot=0, stage=Stage.FAILED)]
    assert allocate_slot(existing, max_slots=3) == 0


def test_in_flight_stages():
    assert Stage.SPEC in IN_FLIGHT_STAGES
    assert Stage.AWAITING_SPEC_REVIEW in IN_FLIGHT_STAGES
    assert Stage.PR_OPEN not in IN_FLIGHT_STAGES
    assert Stage.FAILED not in IN_FLIGHT_STAGES


def test_read_stage_signal(tmp_path: Path):
    agent = tmp_path / ".agent"
    agent.mkdir()
    (agent / "stage.json").write_text(json.dumps({
        "stage": "spec",
        "status": "awaiting-review",
        "note": "draft ready",
        "artifact": "docs/superpowers/specs/2026-07-14-foo-design.md",
    }))
    sig = read_stage_signal(tmp_path)
    assert sig == StageSignal(
        stage="spec",
        status="awaiting-review",
        note="draft ready",
        artifact="docs/superpowers/specs/2026-07-14-foo-design.md",
    )


def test_read_stage_signal_missing_or_corrupt(tmp_path: Path):
    assert read_stage_signal(tmp_path) is None
    agent = tmp_path / ".agent"
    agent.mkdir()
    (agent / "stage.json").write_text("{not json")
    assert read_stage_signal(tmp_path) is None


from dispatcher.state import (clear_waiting, has_waiting, mark_waiting,
                              parked)


def _task(issue=1, stage=Stage.IMPLEMENT, park=""):
    return TaskState(issue=issue, target="t", stage=stage, slot=0,
                     worktree="/tmp/wt", branch=f"agent/task-{issue}",
                     title="x", updated_at="2026-07-21T00:00:00+00:00",
                     park=park)


def test_old_state_file_without_park_fields_loads(tmp_path):
    # Backward-readable across the deploy: pre-park JSON has no new keys.
    import json
    (tmp_path / "task-7.json").write_text(json.dumps({
        "issue": 7, "target": "t", "stage": "implement", "slot": 0,
        "worktree": "/tmp/wt", "branch": "agent/task-7", "title": "x",
        "updated_at": "2026-07-14T00:00:00+00:00"}))
    ts = load(tmp_path, "t", 7)
    assert ts.park == "" and ts.ci_run_id == 0 and ts.park_msg_id == 0
    assert ts.hold_for_attach is False


def test_park_fields_roundtrip(tmp_path):
    save(tmp_path, _task(park=PARK_CI))
    assert load(tmp_path, "t", 1).park == PARK_CI


def test_active_excludes_parked_and_parked_releases_its_slot():
    ts = [_task(issue=1, park=PARK_HUMAN), _task(issue=2)]
    assert [t.issue for t in active(ts)] == [2]
    assert [t.issue for t in parked(ts)] == [1]
    from dispatcher.state import allocate_slot
    assert allocate_slot(ts, max_slots=3) not in (0,)  # slot 0 still held by task 2


def test_parked_helper_covers_all_park_values():
    ts = [_task(issue=1, park=PARK_HUMAN), _task(issue=2, park=PARK_CI),
          _task(issue=3, park=PARK_WAKE), _task(issue=4, park=PARK_LOGIN)]
    assert len(parked(ts)) == 4


def test_login_park_still_counts_as_active():
    # PARK_LOGIN is the one park that keeps its container and tmux session
    # alive, so it must keep consuming capacity too.
    ts = [_task(issue=1, park=PARK_LOGIN), _task(issue=2, park=PARK_HUMAN)]
    assert [t.issue for t in active(ts)] == [1]
    assert [t.issue for t in parked(ts)] == [1, 2]


def test_stage_signal_run_id(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "stage.json").write_text(
        '{"stage": "implement", "status": "awaiting-ci", "run_id": 4242}')
    assert read_stage_signal(tmp_path).run_id == 4242


def test_stage_signal_run_id_defaults_zero(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "stage.json").write_text(
        '{"stage": "spec", "status": "working"}')
    assert read_stage_signal(tmp_path).run_id == 0


def test_waiting_marker_lifecycle(tmp_path):
    assert not has_waiting(tmp_path, "t", 9)
    mark_waiting(tmp_path, "t", 9)
    assert has_waiting(tmp_path, "t", 9)
    clear_waiting(tmp_path, "t", 9)
    assert not has_waiting(tmp_path, "t", 9)
    clear_waiting(tmp_path, "t", 9)  # idempotent


def test_effort_and_labels_roundtrip(tmp_path: Path):
    ts = TaskState(
        issue=101, target="portfolio_eval", stage=Stage.SPEC, slot=0,
        worktree="/wt", branch="agent/task-101", title="Add widget",
        updated_at="2026-07-14T12:00:00+00:00",
        effort=3, labels=("auto", "frontend"),
    )
    save(tmp_path, ts)
    loaded = load(tmp_path, "portfolio_eval", 101)
    assert loaded.effort == 3
    assert loaded.labels == ("auto", "frontend")   # tuple, not list
    assert loaded == ts


def test_state_file_written_before_this_feature_still_loads(tmp_path: Path):
    (tmp_path / "task-55.json").write_text(json.dumps({
        "issue": 55, "target": "portfolio_eval", "stage": "implement", "slot": 1,
        "worktree": "/wt", "branch": "agent/task-55", "title": "Old task",
        "updated_at": "2026-07-01T00:00:00+00:00", "park": "", "ci_run_id": 0,
        "park_msg_id": 0, "hold_for_attach": False,
    }))
    loaded = load(tmp_path, "portfolio_eval", 55)
    assert loaded.effort is None
    assert loaded.labels == ()


from dispatcher.state import clear_attached, has_attached, mark_attached


def test_attached_marker_roundtrip(tmp_path):
    assert not has_attached(tmp_path, "t", 42)
    mark_attached(tmp_path, "t", 42)
    assert has_attached(tmp_path, "t", 42)
    assert (tmp_path / "attached-t-42").exists()
    assert not has_attached(tmp_path, "t", 43)  # per-issue
    clear_attached(tmp_path, "t", 42)
    assert not has_attached(tmp_path, "t", 42)
    clear_attached(tmp_path, "t", 42)  # idempotent


def test_mark_attached_creates_state_dir(tmp_path):
    sd = tmp_path / "fresh"
    mark_attached(sd, "t", 7)
    assert has_attached(sd, "t", 7)


def test_artifact_round_trips(tmp_path):
    ts = TaskState(issue=7, target="alpha", stage=Stage.AWAITING_SPEC_REVIEW,
                   slot=0, worktree="/tmp/wt", branch="task/7", title="t",
                   updated_at="2026-07-28T10:00:00+00:00",
                   artifact="/tmp/wt/docs/superpowers/specs/x-design.md")
    save(tmp_path, ts)
    assert load(tmp_path, "alpha", 7).artifact == ts.artifact


def test_legacy_state_file_without_artifact_loads(tmp_path):
    ts = TaskState(issue=8, target="alpha", stage=Stage.SPEC, slot=0,
                   worktree="/tmp/wt", branch="task/8", title="t",
                   updated_at="2026-07-28T10:00:00+00:00")
    save(tmp_path, ts)
    # Simulate a state file written before the field existed.
    p = tmp_path / "task-alpha-8.json"
    d = json.loads(p.read_text())
    del d["artifact"]
    p.write_text(json.dumps(d))
    assert load(tmp_path, "alpha", 8).artifact == ""


def test_allocate_slot_ignores_slot_less_holders():
    # A gate-parked task holds no slot; slot 0 must stay allocatable.
    existing = [make(issue=1, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT)]
    assert allocate_slot(existing, max_slots=3) == 0


def test_all_slots_free_when_every_holder_is_gate_parked():
    existing = [make(issue=i, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT)
                for i in range(5)]
    assert allocate_slot(existing, max_slots=3) == 0


def test_review_park_frees_capacity_and_counts_as_parked():
    ts = [_task(issue=1, park=PARK_REVIEW)]
    from dispatcher.state import parked, active
    assert parked(ts) == ts
    assert active(ts) == []   # unlike PARK_LOGIN, it does NOT hold capacity


def test_slot_less_state_round_trips(tmp_path):
    ts = replace(make(issue=77, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT),
                 park=PARK_REVIEW)
    save(tmp_path, ts)
    assert load(tmp_path, "portfolio_eval", 77) == ts


def test_park_note_roundtrips_and_defaults_empty(tmp_path):
    t = TaskState(issue=9, target="alpha", stage=Stage.IMPLEMENT, slot=0,
                  worktree="/w", branch="b", title="t",
                  updated_at="2026-07-30T00:00:00+00:00")
    assert t.park_note == ""
    save(tmp_path, replace(t, park_note="need a decision"))
    assert load(tmp_path, "alpha", 9).park_note == "need a decision"


def test_load_tolerates_state_files_without_park_note(tmp_path):
    # Pre-feature task-<N>.json files have no park_note key.
    t = TaskState(issue=9, target="alpha", stage=Stage.IMPLEMENT, slot=0,
                  worktree="/w", branch="b", title="t",
                  updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, t)
    p = tmp_path / "task-alpha-9.json"
    d = json.loads(p.read_text())
    del d["park_note"]
    p.write_text(json.dumps(d))
    assert load(tmp_path, "alpha", 9).park_note == ""


def test_new_stages_exist_and_flight_membership():
    assert Stage.ADDRESS_REVIEW.value == "address-review"
    assert Stage.DONE.value == "done"
    assert Stage.ADDRESS_REVIEW in IN_FLIGHT_STAGES
    assert Stage.DONE not in IN_FLIGHT_STAGES
    assert Stage.PR_OPEN not in IN_FLIGHT_STAGES


def test_pr_fields_default_and_roundtrip(tmp_path):
    ts = TaskState(issue=7, target="t", stage=Stage.PR_OPEN, slot=0,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    assert (ts.pr_number, ts.feedback_cursor, ts.feedback_pending,
            ts.done_at) == (0, "", False, "")
    save(tmp_path, replace(ts, pr_number=12, feedback_pending=True,
                           feedback_cursor="2026-07-30T01:00:00+00:00",
                           done_at="2026-07-30T02:00:00+00:00"))
    got = load(tmp_path, "t", 7)
    assert got.pr_number == 12 and got.feedback_pending is True
    assert got.feedback_cursor == "2026-07-30T01:00:00+00:00"
    assert got.done_at == "2026-07-30T02:00:00+00:00"


def test_old_state_file_without_pr_fields_loads(tmp_path):
    ts = TaskState(issue=8, target="t", stage=Stage.PR_OPEN, slot=0,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    p = tmp_path / "task-t-8.json"
    d = json.loads(p.read_text())
    for k in ("pr_number", "feedback_cursor", "feedback_pending", "done_at"):
        d.pop(k)
    p.write_text(json.dumps(d))
    assert load(tmp_path, "t", 8).pr_number == 0


def test_delete_removes_state_file_and_is_idempotent(tmp_path):
    ts = TaskState(issue=9, target="t", stage=Stage.DONE, slot=-1,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    delete(tmp_path, "t", 9)
    assert load(tmp_path, "t", 9) is None
    delete(tmp_path, "t", 9)  # no raise


def test_consumes_capacity_unparked_in_flight_task():
    assert consumes_capacity(make(stage=Stage.IMPLEMENT))


@pytest.mark.parametrize("stage", sorted(IN_FLIGHT_STAGES, key=lambda s: s.value))
def test_consumes_capacity_true_for_every_in_flight_stage(stage):
    assert consumes_capacity(make(stage=stage))


@pytest.mark.parametrize("stage", [s for s in Stage if s not in IN_FLIGHT_STAGES])
def test_consumes_capacity_false_for_terminal_stages(stage):
    assert not consumes_capacity(make(stage=stage))


@pytest.mark.parametrize("park,expected", [
    ("", True),
    # the one park that keeps a live container: it must keep counting, or the
    # dispatcher would claim fresh work during a box-wide auth expiry
    (PARK_LOGIN, True),
    (PARK_HUMAN, False),
    (PARK_CI, False),
    (PARK_WAKE, False),
    (PARK_REVIEW, False),
])
def test_consumes_capacity_park_truth_table(park, expected):
    t = replace(make(stage=Stage.IMPLEMENT), park=park)
    assert consumes_capacity(t) is expected


def test_active_is_exactly_the_predicate_over_a_mixed_fixture():
    """The extraction must not let active() and the predicate drift apart."""
    tasks = [
        make(issue=1, stage=Stage.IMPLEMENT),
        replace(make(issue=2, stage=Stage.SPEC), park=PARK_LOGIN),
        replace(make(issue=3, stage=Stage.IMPLEMENT), park=PARK_CI),
        replace(make(issue=4, stage=Stage.AWAITING_SPEC_REVIEW), park=PARK_REVIEW),
        make(issue=5, stage=Stage.DONE),
        make(issue=6, stage=Stage.FAILED),
        make(issue=7, stage=Stage.QUEUED),
    ]
    assert active(tasks) == [t for t in tasks if consumes_capacity(t)]
    assert [t.issue for t in active(tasks)] == [1, 2, 7]


def test_task_state_has_no_pending_reply_field():
    from dataclasses import fields
    assert "pending_reply" not in {f.name for f in fields(TaskState)}


def test_load_tolerates_a_retired_pending_reply_key(tmp_path):
    import json
    from dispatcher.state import load, save
    save(tmp_path, TaskState(issue=5, target="t", stage=Stage.IMPLEMENT,
                             slot=0, worktree="/w", branch="b", title="x",
                             updated_at="2026-08-12T00:00:00+00:00"))
    p = tmp_path / "task-t-5.json"
    d = json.loads(p.read_text())
    d["pending_reply"] = "left over from the old schema"
    p.write_text(json.dumps(d))
    assert load(tmp_path, "t", 5).issue == 5


def test_max_slots_is_capacity_plus_headroom():
    from dispatcher.state import max_slots
    assert max_slots(2) == 4
    assert max_slots(3) == 5


def test_allocate_slot_respects_the_passed_max():
    from dispatcher.state import allocate_slot
    existing = [make(issue=1, slot=0), make(issue=2, slot=1)]
    assert allocate_slot(existing, max_slots=2) is None
    assert allocate_slot(existing, max_slots=4) == 2


def test_holds_slot_only_for_live_and_login_parked():
    from dispatcher.state import holds_slot
    assert holds_slot(make(issue=1, slot=0, park="")) is True
    assert holds_slot(make(issue=2, slot=0, park=PARK_LOGIN)) is True
    for park in (PARK_HUMAN, PARK_CI, PARK_WAKE, PARK_REVIEW):
        assert holds_slot(make(issue=3, slot=0, park=park)) is False


def test_max_slots_constant_is_gone():
    import dispatcher.state as state
    assert not hasattr(state, "MAX_SLOTS")


@pytest.mark.parametrize("stage", list(Stage))
@pytest.mark.parametrize(
    "park", ["", PARK_HUMAN, PARK_CI, PARK_WAKE, PARK_LOGIN, PARK_REVIEW])
def test_slot_holders_are_exactly_the_capacity_consumers(stage, park):
    """holds_slot and consumes_capacity are distinct CONCEPTS that currently
    coincide, and the coincidence is load-bearing: because the slot-holder set
    equals the capacity-consumer set, the capacity gate guarantees at most
    capacity - 1 slots are held when allocate_slot runs, which is what makes a
    ceiling of max_slots(capacity) == capacity + 2 sufficient — allocate_slot
    can never return None.

    Let the two diverge (e.g. give PARK_CI its slot back to holds_slot alone)
    and the starvation this branch exists to kill returns silently: parked
    tasks would pin every number while capacity still shows headroom, and no
    other test would fail. Do NOT collapse the functions to satisfy this test;
    the test is the pin.
    """
    from dispatcher.state import holds_slot
    t = replace(make(stage=stage), park=park)
    assert holds_slot(t) is consumes_capacity(t)


def test_the_two_predicates_agree_over_the_mixed_fixture():
    from dispatcher.state import holds_slot
    tasks = [
        make(issue=1, stage=Stage.IMPLEMENT),
        replace(make(issue=2, stage=Stage.SPEC), park=PARK_LOGIN),
        replace(make(issue=3, stage=Stage.IMPLEMENT), park=PARK_CI),
        replace(make(issue=4, stage=Stage.AWAITING_SPEC_REVIEW), park=PARK_REVIEW),
        make(issue=5, stage=Stage.DONE),
        make(issue=6, stage=Stage.FAILED),
        make(issue=7, stage=Stage.QUEUED),
    ]
    assert ([t.issue for t in tasks if holds_slot(t)]
            == [t.issue for t in tasks if consumes_capacity(t)] == [1, 2, 7])


# Tests for (target, issue) keying with legacy fallback
def _ts(issue, target, **kw):
    from dispatcher.state import Stage, TaskState
    base = dict(issue=issue, target=target, stage=Stage.SPEC, slot=0,
                worktree=f"/wt/{target}-{issue}", branch=f"agent/task-{issue}",
                title="t", updated_at="2026-08-26T00:00:00+00:00")
    base.update(kw)
    return TaskState(**base)


def test_same_issue_number_two_targets_coexist(tmp_path):
    from dispatcher import state
    state.save(tmp_path, _ts(42, "portfolio_eval"))
    state.save(tmp_path, _ts(42, "agent_ops"))
    assert (tmp_path / "task-portfolio_eval-42.json").exists()
    assert (tmp_path / "task-agent_ops-42.json").exists()
    loaded = state.load_all(tmp_path)
    assert {(t.target, t.issue) for t in loaded} == {
        ("portfolio_eval", 42), ("agent_ops", 42)}
    assert state.load(tmp_path, "agent_ops", 42).worktree == "/wt/agent_ops-42"


def test_legacy_state_file_read_and_upgraded_on_save(tmp_path):
    import json
    from dispatcher import state
    legacy = tmp_path / "task-7.json"
    d = state.asdict(_ts(7, "portfolio_eval")); d["stage"] = "spec"
    legacy.write_text(json.dumps(d))
    # load_all sees it; load() with the right target finds it, wrong target misses
    assert [t.issue for t in state.load_all(tmp_path)] == [7]
    assert state.load(tmp_path, "portfolio_eval", 7) is not None
    assert state.load(tmp_path, "agent_ops", 7) is None
    # save() writes new-style and removes the legacy twin
    state.save(tmp_path, _ts(7, "portfolio_eval"))
    assert not legacy.exists()
    assert (tmp_path / "task-portfolio_eval-7.json").exists()


def test_delete_removes_both_stylings(tmp_path):
    import json
    from dispatcher import state
    d = state.asdict(_ts(9, "portfolio_eval")); d["stage"] = "spec"
    (tmp_path / "task-9.json").write_text(json.dumps(d))
    state.delete(tmp_path, "portfolio_eval", 9)
    assert not (tmp_path / "task-9.json").exists()


def test_waiting_marker_target_scoped_with_legacy_fallback(tmp_path):
    from dispatcher import state
    state.mark_waiting(tmp_path, "agent_ops", 5)
    assert state.has_waiting(tmp_path, "agent_ops", 5)
    assert not state.has_waiting(tmp_path, "portfolio_eval", 5)
    (tmp_path / "waiting-6").touch()          # legacy marker
    assert state.has_waiting(tmp_path, "portfolio_eval", 6)
    state.clear_waiting(tmp_path, "portfolio_eval", 6)
    assert not (tmp_path / "waiting-6").exists()
