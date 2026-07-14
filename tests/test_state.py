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
