import json
from dataclasses import replace
from pathlib import Path

from dispatcher.state import (
    IN_FLIGHT_STAGES,
    NO_SLOT,
    PARK_REVIEW,
    Stage,
    StageSignal,
    TaskState,
    allocate_slot,
    delete,
    load,
    load_all,
    read_stage_signal,
    save,
)


def make(issue=101, stage=Stage.SPEC, slot=0):
    return TaskState(
        issue=issue,
        target="portfolio_eval",
        stage=stage,
        slot=slot,
        worktree=f"/home/agent/repos/portfolio_eval.worktrees/task-{issue}",
        branch=f"agent/task-{issue}",
        title="Add widget",
        updated_at="2026-07-14T12:00:00+00:00",
    )


def test_save_load_roundtrip(tmp_path: Path):
    ts = make()
    save(tmp_path, ts)
    assert (tmp_path / "task-101.json").exists()
    loaded = load(tmp_path, 101)
    assert loaded == ts
    assert loaded.stage is Stage.SPEC


def test_load_missing_returns_none(tmp_path: Path):
    assert load(tmp_path, 999) is None


def test_load_all_sorted_by_issue(tmp_path: Path):
    save(tmp_path, make(issue=202, slot=1))
    save(tmp_path, make(issue=101, slot=0))
    assert [t.issue for t in load_all(tmp_path)] == [101, 202]


def test_allocate_slot_picks_first_free():
    existing = [make(issue=1, slot=0), make(issue=2, slot=2)]
    assert allocate_slot(existing) == 1


def test_allocate_slot_full_returns_none():
    existing = [make(issue=i, slot=i) for i in range(3)]
    assert allocate_slot(existing) is None


def test_terminal_stages_do_not_hold_slots():
    # a FAILED task's slot is reusable
    existing = [make(issue=1, slot=0, stage=Stage.FAILED)]
    assert allocate_slot(existing) == 0


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


from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_LOGIN, PARK_WAKE,
                              Stage, TaskState,
                              active, clear_waiting, has_waiting, load,
                              mark_waiting, parked, read_stage_signal, save)


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
    ts = load(tmp_path, 7)
    assert ts.park == "" and ts.ci_run_id == 0 and ts.park_msg_id == 0
    assert ts.pending_reply == "" and ts.hold_for_attach is False


def test_park_fields_roundtrip(tmp_path):
    save(tmp_path, _task(park=PARK_CI))
    assert load(tmp_path, 1).park == PARK_CI


def test_active_excludes_parked_but_parked_holds_slot():
    ts = [_task(issue=1, park=PARK_HUMAN), _task(issue=2)]
    assert [t.issue for t in active(ts)] == [2]
    assert [t.issue for t in parked(ts)] == [1]
    from dispatcher.state import allocate_slot
    assert allocate_slot(ts) not in (0,)  # slot 0 still held by both


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
    assert not has_waiting(tmp_path, 9)
    mark_waiting(tmp_path, 9)
    assert has_waiting(tmp_path, 9)
    clear_waiting(tmp_path, 9)
    assert not has_waiting(tmp_path, 9)
    clear_waiting(tmp_path, 9)  # idempotent


def test_effort_and_labels_roundtrip(tmp_path: Path):
    ts = TaskState(
        issue=101, target="portfolio_eval", stage=Stage.SPEC, slot=0,
        worktree="/wt", branch="agent/task-101", title="Add widget",
        updated_at="2026-07-14T12:00:00+00:00",
        effort=3, labels=("auto", "frontend"),
    )
    save(tmp_path, ts)
    loaded = load(tmp_path, 101)
    assert loaded.effort == 3
    assert loaded.labels == ("auto", "frontend")   # tuple, not list
    assert loaded == ts


def test_state_file_written_before_this_feature_still_loads(tmp_path: Path):
    (tmp_path / "task-55.json").write_text(json.dumps({
        "issue": 55, "target": "portfolio_eval", "stage": "implement", "slot": 1,
        "worktree": "/wt", "branch": "agent/task-55", "title": "Old task",
        "updated_at": "2026-07-01T00:00:00+00:00", "park": "", "ci_run_id": 0,
        "park_msg_id": 0, "pending_reply": "", "hold_for_attach": False,
    }))
    loaded = load(tmp_path, 55)
    assert loaded.effort is None
    assert loaded.labels == ()


from dispatcher.state import clear_attached, has_attached, mark_attached


def test_attached_marker_roundtrip(tmp_path):
    assert not has_attached(tmp_path, 42)
    mark_attached(tmp_path, 42)
    assert has_attached(tmp_path, 42)
    assert (tmp_path / "attached-42").exists()
    assert not has_attached(tmp_path, 43)  # per-issue
    clear_attached(tmp_path, 42)
    assert not has_attached(tmp_path, 42)
    clear_attached(tmp_path, 42)  # idempotent


def test_mark_attached_creates_state_dir(tmp_path):
    sd = tmp_path / "fresh"
    mark_attached(sd, 7)
    assert has_attached(sd, 7)


def test_artifact_round_trips(tmp_path):
    ts = TaskState(issue=7, target="alpha", stage=Stage.AWAITING_SPEC_REVIEW,
                   slot=0, worktree="/tmp/wt", branch="task/7", title="t",
                   updated_at="2026-07-28T10:00:00+00:00",
                   artifact="/tmp/wt/docs/superpowers/specs/x-design.md")
    save(tmp_path, ts)
    assert load(tmp_path, 7).artifact == ts.artifact


def test_legacy_state_file_without_artifact_loads(tmp_path):
    ts = TaskState(issue=8, target="alpha", stage=Stage.SPEC, slot=0,
                   worktree="/tmp/wt", branch="task/8", title="t",
                   updated_at="2026-07-28T10:00:00+00:00")
    save(tmp_path, ts)
    # Simulate a state file written before the field existed.
    p = tmp_path / "task-8.json"
    d = json.loads(p.read_text())
    del d["artifact"]
    p.write_text(json.dumps(d))
    assert load(tmp_path, 8).artifact == ""


def test_allocate_slot_ignores_slot_less_holders():
    # A gate-parked task holds no slot; slot 0 must stay allocatable.
    existing = [make(issue=1, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT)]
    assert allocate_slot(existing) == 0


def test_all_slots_free_when_every_holder_is_gate_parked():
    existing = [make(issue=i, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT)
                for i in range(5)]
    assert allocate_slot(existing) == 0


def test_review_park_frees_capacity_and_counts_as_parked():
    ts = [_task(issue=1, park=PARK_REVIEW)]
    from dispatcher.state import parked, active
    assert parked(ts) == ts
    assert active(ts) == []   # unlike PARK_LOGIN, it does NOT hold capacity


def test_slot_less_state_round_trips(tmp_path):
    ts = replace(make(issue=77, stage=Stage.AWAITING_SPEC_REVIEW, slot=NO_SLOT),
                 park=PARK_REVIEW)
    save(tmp_path, ts)
    assert load(tmp_path, 77) == ts


def test_park_note_roundtrips_and_defaults_empty(tmp_path):
    t = TaskState(issue=9, target="alpha", stage=Stage.IMPLEMENT, slot=0,
                  worktree="/w", branch="b", title="t",
                  updated_at="2026-07-30T00:00:00+00:00")
    assert t.park_note == ""
    save(tmp_path, replace(t, park_note="need a decision"))
    assert load(tmp_path, 9).park_note == "need a decision"


def test_load_tolerates_state_files_without_park_note(tmp_path):
    # Pre-feature task-<N>.json files have no park_note key.
    t = TaskState(issue=9, target="alpha", stage=Stage.IMPLEMENT, slot=0,
                  worktree="/w", branch="b", title="t",
                  updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, t)
    p = tmp_path / "task-9.json"
    d = json.loads(p.read_text())
    del d["park_note"]
    p.write_text(json.dumps(d))
    assert load(tmp_path, 9).park_note == ""


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
    got = load(tmp_path, 7)
    assert got.pr_number == 12 and got.feedback_pending is True
    assert got.feedback_cursor == "2026-07-30T01:00:00+00:00"
    assert got.done_at == "2026-07-30T02:00:00+00:00"


def test_old_state_file_without_pr_fields_loads(tmp_path):
    ts = TaskState(issue=8, target="t", stage=Stage.PR_OPEN, slot=0,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    p = tmp_path / "task-8.json"
    d = json.loads(p.read_text())
    for k in ("pr_number", "feedback_cursor", "feedback_pending", "done_at"):
        d.pop(k)
    p.write_text(json.dumps(d))
    assert load(tmp_path, 8).pr_number == 0


def test_delete_removes_state_file_and_is_idempotent(tmp_path):
    ts = TaskState(issue=9, target="t", stage=Stage.DONE, slot=-1,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    delete(tmp_path, 9)
    assert load(tmp_path, 9) is None
    delete(tmp_path, 9)  # no raise
