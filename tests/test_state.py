import json
from pathlib import Path

from dispatcher.state import (
    IN_FLIGHT_STAGES,
    Stage,
    StageSignal,
    TaskState,
    allocate_slot,
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


from dispatcher.state import (PARK_CI, PARK_HUMAN, PARK_WAKE, Stage, TaskState,
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
          _task(issue=3, park=PARK_WAKE)]
    assert len(parked(ts)) == 3 and active(ts) == []


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
