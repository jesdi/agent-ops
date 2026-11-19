import json
from dataclasses import replace
from pathlib import Path

import pytest

from dispatcher.state import (
    NO_SLOT,
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
from dispatcher.status import Cause, FailReason, Kind, Status
from tests.statuses import (CANCELED, DONE, PR, RUNNING, answers, ci, failed, held, human,
                            login, review, waking)


def make(issue=101, stage=Stage.SPEC, slot=0, **kw):
    return TaskState(
        issue=issue,
        repository="portfolio_eval",
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


def test_ended_tasks_do_not_hold_slots():
    # a failed task's slot is reusable
    existing = [make(issue=1, slot=0, status=failed())]
    assert allocate_slot(existing, max_slots=3) == 0


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


from dispatcher.state import clear_turn_markers, has_waiting, mark_waiting


def test_a_state_file_without_the_optional_fields_loads(tmp_path):
    import json
    (tmp_path / "task-7.json").write_text(json.dumps({
        "issue": 7, "repository": "t", "stage": "implement", "slot": 0,
        "worktree": "/tmp/wt", "branch": "agent/task-7", "title": "x",
        "updated_at": "2026-07-14T00:00:00+00:00",
        "status": {"kind": "running", "detail": {}}}))
    ts = load(tmp_path, "t", 7)
    assert ts.status == RUNNING and ts.park_msg_id == 0
    assert ts.hold_for_attach is False


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


def _signal_file(tmp_path):
    (tmp_path / ".agent").mkdir()
    return tmp_path / ".agent" / "stage.json"


def test_stage_signal_at_a_fifo_is_none_and_does_not_block(tmp_path):
    import os
    import threading
    os.mkfifo(_signal_file(tmp_path))
    out = []
    th = threading.Thread(target=lambda: out.append(read_stage_signal(tmp_path)),
                          daemon=True)
    th.start()
    th.join(2)
    assert not th.is_alive() and out == [None]


def test_stage_signal_symlink_is_not_followed(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"stage": "plan", "status": "done"}')
    _signal_file(tmp_path).symlink_to(outside)
    assert read_stage_signal(tmp_path) is None


def test_oversized_stage_signal_is_ignored(tmp_path):
    from dispatcher.state import STAGE_SIGNAL_MAX_BYTES
    f = _signal_file(tmp_path)
    body = '{"stage": "plan", "status": "done", "note": "%s"}'
    f.write_text(body % ("x" * (STAGE_SIGNAL_MAX_BYTES - len(body) + 2)))
    assert f.stat().st_size == STAGE_SIGNAL_MAX_BYTES
    assert read_stage_signal(tmp_path).status == "done"
    f.write_text(body % ("x" * STAGE_SIGNAL_MAX_BYTES))
    assert read_stage_signal(tmp_path) is None


@pytest.mark.parametrize("note", [None, 3, ["a"], {"a": 1}, True])
def test_stage_signal_note_that_is_not_a_string_reads_as_empty(tmp_path, note):
    _signal_file(tmp_path).write_text(json.dumps(
        {"stage": "plan", "status": "blocked", "note": note}))
    assert read_stage_signal(tmp_path) == StageSignal("plan", "blocked", note="")


def test_deeply_nested_stage_signal_is_none(tmp_path):
    _signal_file(tmp_path).write_text("[" * 100_000)
    assert read_stage_signal(tmp_path) is None


@pytest.mark.parametrize("field", ["run_id", "round"])
@pytest.mark.parametrize("raw", ["1e400", "-1e400", "1.5", "true", '"1e400"', '"-1"',
                                 '"' + "9" * 400 + '"', "-1", str(2 ** 70), "[1]", "{}"])
def test_stage_signal_with_a_number_field_that_is_no_sane_integer_is_unreadable(
        tmp_path, field, raw):
    _signal_file(tmp_path).write_text(
        '{"stage": "implement", "status": "awaiting-ci", "%s": %s}' % (field, raw))
    assert read_stage_signal(tmp_path) is None


@pytest.mark.parametrize("raw, value", [("0", 0), ("null", 0), ('""', 0), ("4242", 4242),
                                        ('"12"', 12), ("17123456789", 17123456789)])
def test_stage_signal_integer_fields_read(tmp_path, raw, value):
    _signal_file(tmp_path).write_text(
        '{"stage": "implement", "status": "awaiting-ci", "run_id": %s, "round": %s}'
        % (raw, raw))
    sig = read_stage_signal(tmp_path)
    assert (sig.run_id, sig.round) == (value, value)


def test_waiting_marker_lifecycle(tmp_path):
    assert not has_waiting(tmp_path, "t", 9)
    mark_waiting(tmp_path, "t", 9)
    assert has_waiting(tmp_path, "t", 9)
    clear_turn_markers(tmp_path, "t", 9)
    assert not has_waiting(tmp_path, "t", 9)
    clear_turn_markers(tmp_path, "t", 9)  # idempotent


def test_effort_and_labels_roundtrip(tmp_path: Path):
    ts = TaskState(
        issue=101, repository="portfolio_eval", stage=Stage.SPEC, slot=0,
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
        "issue": 55, "repository": "portfolio_eval", "stage": "implement", "slot": 1,
        "worktree": "/wt", "branch": "agent/task-55", "title": "Old task",
        "updated_at": "2026-07-01T00:00:00+00:00", "park_msg_id": 0,
        "hold_for_attach": False, "status": {"kind": "running", "detail": {}},
    }))
    loaded = load(tmp_path, "portfolio_eval", 55)
    assert loaded.effort is None
    assert loaded.labels == ()


def test_allocate_slot_ignores_slot_less_holders():
    # A gate-parked task holds no slot; slot 0 must stay allocatable.
    existing = [make(issue=1, stage=Stage.PLAN, status=review(), slot=NO_SLOT)]
    assert allocate_slot(existing, max_slots=3) == 0


def test_all_slots_free_when_every_holder_is_gate_parked():
    existing = [make(issue=i, stage=Stage.PLAN, status=review(), slot=NO_SLOT)
                for i in range(5)]
    assert allocate_slot(existing, max_slots=3) == 0


def test_slot_less_state_round_trips(tmp_path):
    ts = make(issue=77, stage=Stage.PLAN, status=review(), slot=NO_SLOT)
    save(tmp_path, ts)
    assert load(tmp_path, "portfolio_eval", 77) == ts


def test_pr_fields_default_and_roundtrip(tmp_path):
    ts = TaskState(issue=7, repository="t", stage=Stage.PR_OPEN, status=PR, slot=NO_SLOT,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    assert (ts.pr_number, ts.feedback_cursor, ts.done_at) == (0, "", "")
    save(tmp_path, replace(ts, pr_number=12, status=waking(PR, "attention(feedback)"),
                           feedback_cursor="2026-07-30T01:00:00+00:00",
                           done_at="2026-07-30T02:00:00+00:00"))
    got = load(tmp_path, "t", 7)
    assert got.pr_number == 12 and got.status == waking(PR, "attention(feedback)")
    assert got.feedback_cursor == "2026-07-30T01:00:00+00:00"
    assert got.done_at == "2026-07-30T02:00:00+00:00"


def test_old_state_file_without_pr_fields_loads(tmp_path):
    ts = TaskState(issue=8, repository="t", stage=Stage.PR_OPEN, status=PR, slot=NO_SLOT,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    p = tmp_path / "task-t-8.json"
    d = json.loads(p.read_text())
    for k in ("pr_number", "feedback_cursor", "done_at"):
        d.pop(k)
    p.write_text(json.dumps(d))
    assert load(tmp_path, "t", 8).pr_number == 0


def test_delete_removes_state_file_and_is_idempotent(tmp_path):
    ts = TaskState(issue=9, repository="t", stage=Stage.PR_OPEN, status=DONE, slot=-1,
                   worktree="w", branch="b", title="x",
                   updated_at="2026-07-30T00:00:00+00:00")
    save(tmp_path, ts)
    delete(tmp_path, "t", 9)
    assert load(tmp_path, "t", 9) is None
    delete(tmp_path, "t", 9)  # no raise


def test_task_state_has_no_pending_reply_field():
    from dataclasses import fields
    assert "pending_reply" not in {f.name for f in fields(TaskState)}


def test_load_tolerates_a_retired_pending_reply_key(tmp_path):
    import json
    from dispatcher.state import load, save
    save(tmp_path, TaskState(issue=5, repository="t", stage=Stage.IMPLEMENT,
                             slot=0, worktree="/w", branch="b", title="x",
                             updated_at="2026-08-12T00:00:00+00:00"))
    p = tmp_path / "task-t-5.json"
    d = json.loads(p.read_text())
    d["pending_reply"] = "left over from the old schema"
    p.write_text(json.dumps(d))
    assert load(tmp_path, "t", 5).issue == 5


def test_a_task_file_written_before_the_asked_field_loads_as_not_asked(tmp_path):
    import json
    from dispatcher.state import load, save
    save(tmp_path, TaskState(issue=5, repository="t", stage=Stage.SPEC,
                             slot=0, worktree="/w", branch="b", title="x",
                             updated_at="2026-08-12T00:00:00+00:00", asked=True))
    p = tmp_path / "task-t-5.json"
    d = json.loads(p.read_text())
    assert d.pop("asked") is True
    p.write_text(json.dumps(d))
    assert load(tmp_path, "t", 5).asked is False


def test_max_slots_is_capacity_plus_headroom():
    from dispatcher.state import max_slots
    assert max_slots(2) == 4
    assert max_slots(3) == 5


def test_allocate_slot_respects_the_passed_max():
    from dispatcher.state import allocate_slot
    existing = [make(issue=1, slot=0), make(issue=2, slot=1)]
    assert allocate_slot(existing, max_slots=2) is None
    assert allocate_slot(existing, max_slots=4) == 2


def test_max_slots_constant_is_gone():
    import dispatcher.state as state
    assert not hasattr(state, "MAX_SLOTS")


# Tests for (repository, issue) keying with legacy fallback
def _ts(issue, repository, **kw):
    from dispatcher.state import Stage, TaskState
    base = dict(issue=issue, repository=repository, stage=Stage.SPEC, slot=0,
                worktree=f"/wt/{repository}-{issue}", branch=f"agent/task-{issue}",
                title="t", updated_at="2026-08-26T00:00:00+00:00")
    base.update(kw)
    return TaskState(**base)


def test_same_issue_number_two_repositories_coexist(tmp_path):
    from dispatcher import state
    state.save(tmp_path, _ts(42, "portfolio_eval"))
    state.save(tmp_path, _ts(42, "agent_ops"))
    assert (tmp_path / "task-portfolio_eval-42.json").exists()
    assert (tmp_path / "task-agent_ops-42.json").exists()
    loaded = state.load_all(tmp_path)
    assert {(t.repository, t.issue) for t in loaded} == {
        ("portfolio_eval", 42), ("agent_ops", 42)}
    assert state.load(tmp_path, "agent_ops", 42).worktree == "/wt/agent_ops-42"


def test_legacy_state_file_read_and_upgraded_on_save(tmp_path):
    import json
    from dispatcher import state
    legacy = tmp_path / "task-7.json"
    d = state.asdict(_ts(7, "portfolio_eval")); d["stage"] = "spec"
    legacy.write_text(json.dumps(d))
    # load_all sees it; load() with the right repository finds it, wrong repository misses
    assert [t.issue for t in state.load_all(tmp_path)] == [7]
    assert state.load(tmp_path, "portfolio_eval", 7) is not None
    assert state.load(tmp_path, "agent_ops", 7) is None
    # save() writes new-style and removes the legacy twin
    state.save(tmp_path, _ts(7, "portfolio_eval"))
    assert not legacy.exists()
    assert (tmp_path / "task-portfolio_eval-7.json").exists()


def test_save_keeps_another_repositories_legacy_file(tmp_path):
    import json
    from dispatcher import state
    d = state.asdict(_ts(7, "alpha")); d["stage"] = "spec"
    (tmp_path / "task-7.json").write_text(json.dumps(d))
    state.save(tmp_path, _ts(7, "beta"))
    assert state.load(tmp_path, "alpha", 7) is not None
    assert state.load(tmp_path, "beta", 7) is not None


def test_delete_removes_both_stylings(tmp_path):
    import json
    from dispatcher import state
    d = state.asdict(_ts(9, "portfolio_eval")); d["stage"] = "spec"
    (tmp_path / "task-9.json").write_text(json.dumps(d))
    state.delete(tmp_path, "portfolio_eval", 9)
    assert not (tmp_path / "task-9.json").exists()


def test_waiting_marker_repository_scoped_with_legacy_fallback(tmp_path):
    from dispatcher import state
    state.mark_waiting(tmp_path, "agent_ops", 5)
    assert state.has_waiting(tmp_path, "agent_ops", 5)
    assert not state.has_waiting(tmp_path, "portfolio_eval", 5)
    (tmp_path / "waiting-6").touch()          # legacy marker
    assert state.has_waiting(tmp_path, "portfolio_eval", 6)
    state.clear_turn_markers(tmp_path, "portfolio_eval", 6)
    assert not (tmp_path / "waiting-6").exists()


def test_loop_caps_defaults_match_the_spec():
    from dispatcher.state import LoopCaps
    assert LoopCaps() == LoopCaps(review=2, gate=2, e2e=3, ci=3)


def test_orchestration_fields_default_and_round_trip(tmp_path):
    from dataclasses import replace
    from dispatcher.state import Stage, TaskState, load, save
    ts = TaskState(issue=9, repository="t", stage=Stage.IMPLEMENT, slot=0,
                   worktree="/wt", branch="agent/task-9", title="t",
                   updated_at="2026-09-07T00:00:00+00:00")
    assert (ts.spec_path, ts.ticket_count) == ("", 0)
    assert (ts.review_rounds, ts.gate_rounds, ts.e2e_rounds, ts.ci_rounds) == (0, 0, 0, 0)
    assert (ts.check_cursor, ts.conflict_cursor) == ("", "")
    full = replace(ts, spec_path="docs/specs/x.md",
                   ticket_count=5, gate_rounds=1, e2e_rounds=2, ci_rounds=3,
                   review_rounds=1, check_cursor="2026-09-07T01:00:00Z",
                   conflict_cursor="abc123")
    save(tmp_path, full)
    assert load(tmp_path, "t", 9) == full


def test_legacy_state_file_without_orchestration_fields_loads(tmp_path):
    import json
    from dispatcher.state import Stage, load
    (tmp_path / "task-t-9.json").write_text(json.dumps({
        "issue": 9, "repository": "t", "stage": "implement", "slot": 0,
        "worktree": "/wt", "branch": "b", "title": "t",
        "updated_at": "2026-09-07T00:00:00+00:00",
        "status": {"kind": "running", "detail": {}}}))
    ts = load(tmp_path, "t", 9)
    assert ts.stage is Stage.IMPLEMENT and ts.ticket_count == 0


def test_read_stage_signal_parses_loop_and_round(tmp_path):
    import json
    from dispatcher.state import read_stage_signal
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working", "loop": "gate", "round": "2"}))
    sig = read_stage_signal(tmp_path)
    assert (sig.loop, sig.round) == ("gate", 2)
    (tmp_path / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": "working"}))
    sig = read_stage_signal(tmp_path)
    assert (sig.loop, sig.round) == ("", 0)


# ---------------------------------------------------------------------------
# Slice 3: operator_request field — decode-seam tests
# ---------------------------------------------------------------------------

def test_track_and_picks_round_trip(tmp_path: Path):
    ts = make(track="security", picks={"spec": "anthropic/claude-fable-5-1@high"},
              spec_retries=1)
    save(tmp_path, ts)
    got = load(tmp_path, "portfolio_eval", 101)
    assert got.track == "security"
    assert got.picks == {"spec": "anthropic/claude-fable-5-1@high"}
    assert got.spec_retries == 1


def test_legacy_record_without_track_fields_loads_with_defaults(tmp_path: Path):
    import dataclasses
    d = dataclasses.asdict(make())
    d["stage"] = "spec"
    for k in ("track", "picks", "spec_retries"):
        d.pop(k)
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    got = load(tmp_path, "portfolio_eval", 101)
    assert (got.track, got.picks, got.spec_retries) == ("", {}, 0)


def test_read_stage_signal_carries_track(tmp_path: Path):
    agent = tmp_path / ".agent"
    agent.mkdir()
    (agent / "stage.json").write_text(json.dumps({
        "stage": "spec", "status": "awaiting-review", "artifact": "s.md",
        "track": "standard"}))
    assert read_stage_signal(tmp_path).track == "standard"
    (agent / "stage.json").write_text(json.dumps({"stage": "spec", "status": "done"}))
    assert read_stage_signal(tmp_path).track == ""


def test_next_stage_wakes_a_pr_open_task_into_address_review():
    from dispatcher.state import next_stage
    assert next_stage(make(stage=Stage.PR_OPEN, status=PR)) == "address-review"
    assert next_stage(make(stage=Stage.IMPLEMENT)) == "implement"
    assert next_stage(make(stage=Stage.REVIEW, status=failed())) == "review"


def test_implement_providers_round_trip(tmp_path: Path):
    save(tmp_path, make(implement_providers=["openai", "anthropic"]))
    assert load(tmp_path, "portfolio_eval", 101).implement_providers == ["openai", "anthropic"]


def test_old_shape_implement_pick_counts_as_the_recorded_provider(tmp_path: Path):
    import dataclasses
    d = dataclasses.asdict(make(picks={"implement": "openai/gpt-sol@medium"}))
    d["stage"] = "implement"
    d.pop("implement_providers")
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    assert load(tmp_path, "portfolio_eval", 101).implement_providers == ["openai"]


def test_old_shape_without_an_implement_pick_records_nothing(tmp_path: Path):
    import dataclasses
    d = dataclasses.asdict(make())
    d["stage"] = "spec"
    d.pop("implement_providers")
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    assert load(tmp_path, "portfolio_eval", 101).implement_providers == []


def test_empty_recorded_providers_with_an_implement_pick_count_the_pick(tmp_path: Path):
    save(tmp_path, make(stage=Stage.IMPLEMENT, picks={"implement": "openai/gpt-sol@medium"}))
    assert load(tmp_path, "portfolio_eval", 101).implement_providers == ["openai"]


def test_state_lock_flocks_the_named_file_under_state_dir(tmp_path):
    import fcntl
    from dispatcher.state import state_lock
    with state_lock(tmp_path / "s", "onboarding.lock"):
        with open(tmp_path / "s" / "onboarding.lock", "w") as fh, \
                pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TimeoutError, match="onboarding.lock still held"):
            with state_lock(tmp_path / "s", "onboarding.lock", timeout=0.2):
                pass
    with state_lock(tmp_path / "s", "onboarding.lock", timeout=0.2):
        pass


# ---------------------------------------------------------------------------
# #132 ticket 06: stage and status are the two stored fields
# ---------------------------------------------------------------------------

RETIRED_KEYS = {"park", "park_reason", "park_note", "ci_run_id", "crashed_stage",
                "unlaunched", "operator_request"}


def _file(tmp_path, issue=101) -> dict:
    return json.loads((tmp_path / f"task-portfolio_eval-{issue}.json").read_text())


def test_a_written_file_has_stage_and_status_and_no_park_field(tmp_path):
    save(tmp_path, make(stage=Stage.SPEC, status=answers(".agent/q.html", "which db?"),
                        slot=NO_SLOT))
    d = _file(tmp_path)
    assert d["stage"] == "spec"
    assert d["status"] == {"kind": "parked-answers",
                           "detail": {"path": ".agent/q.html", "fingerprint": "",
                                      "note": "which db?"}}
    assert not RETIRED_KEYS & set(d)


@pytest.mark.parametrize("status", [failed("task-crash"), CANCELED, DONE])
def test_an_ended_task_keeps_its_stage_and_round_trips_its_status(tmp_path, status):
    save(tmp_path, make(stage=Stage.IMPLEMENT, status=status))
    got = load(tmp_path, "portfolio_eval", 101)
    assert (got.stage, got.status, got.slot) == (Stage.IMPLEMENT, status, NO_SLOT)


def test_a_waking_status_round_trips_with_its_origin(tmp_path):
    status = waking(review(".agent/review.html", "plan ready"), "reply")
    save(tmp_path, make(stage=Stage.PLAN, status=status))
    assert _file(tmp_path)["status"] == {"kind": "waking", "detail": {
        "from": {"kind": "parked-review", "detail": {
            "page": ".agent/review.html", "fingerprint": "", "note": "plan ready"}},
        "cause": "reply"}}
    assert load(tmp_path, "portfolio_eval", 101).status == status


@pytest.mark.parametrize("stage", ["awaiting-plan-review", "failed", "done", "canceled",
                                   "blocked", "stalled-on-budget"])
def test_a_retired_stage_is_not_converted_on_load(tmp_path, stage):
    save(tmp_path, make())
    d = _file(tmp_path)
    d["stage"] = stage
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    with pytest.raises(ValueError):
        load(tmp_path, "portfolio_eval", 101)


def test_a_file_with_no_status_is_not_loaded(tmp_path):
    save(tmp_path, make())
    d = _file(tmp_path)
    del d["status"]
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    with pytest.raises(KeyError):
        load(tmp_path, "portfolio_eval", 101)


def test_a_retired_field_on_a_new_shape_file_is_dropped(tmp_path):
    save(tmp_path, make())
    d = _file(tmp_path)
    d.update(park="parked", park_reason="decision", park_note="q", ci_run_id=9,
             crashed_stage="spec", unlaunched=True, feedback_pending=True,
             operator_request={"kind": "answers", "path": "q.html"})
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    assert load(tmp_path, "portfolio_eval", 101) == make()


def test_a_saved_task_file_has_no_feedback_pending_and_no_attention_key(tmp_path):
    save(tmp_path, make())
    assert not {"feedback_pending", "attention"} & _file(tmp_path).keys()


def test_an_old_task_file_with_attention_loads_without_it(tmp_path):
    save(tmp_path, make())
    d = _file(tmp_path)
    d["attention"] = "conflict"
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    assert load(tmp_path, "portfolio_eval", 101) == make()


@pytest.mark.parametrize("stage, status", [
    (Stage.SPEC, answers("")),
    (Stage.PLAN, review("")),
    (Stage.PLAN, held("")),
    (Stage.IMPLEMENT, ci(0)),
    (Stage.IMPLEMENT, ci(-3)),
    (Stage.IMPLEMENT, Status(Kind.PARKED_HUMAN, reason=FailReason.KILLED)),
    (Stage.IMPLEMENT, Status(Kind.FAILED)),
    (Stage.IMPLEMENT, Status(Kind.WAKING, origin=waking(), cause=Cause.REPLY)),
    (Stage.IMPLEMENT, Status(Kind.WAKING, origin=DONE, cause=Cause.REPLY)),
    (Stage.IMPLEMENT, Status(Kind.WAKING, origin=CANCELED, cause=Cause.REPLY)),
    (Stage.IMPLEMENT, Status(Kind.WAKING, cause=Cause.REPLY)),
    (Stage.QUEUED, Status(Kind.WAKING, origin=human(), cause=Cause.LAUNCH)),
    (Stage.IMPLEMENT, Status(Kind.WAKING, origin=human())),
    (Stage.IMPLEMENT, held()),
    (Stage.SPEC, review()),
    (Stage.SPEC, PR),
    (Stage.SPEC, waking(cause="launch")),
    (Stage.IMPLEMENT, waking(cause="next-stage")),
    (Stage.IMPLEMENT, waking(review(), "reply")),
])
def test_the_save_refuses_a_status_that_breaks_a_constraint(tmp_path, stage, status):
    with pytest.raises(ValueError):
        save(tmp_path, make(stage=stage, status=status))
    assert not (tmp_path / "task-portfolio_eval-101.json").exists()


def test_an_unknown_status_kind_is_not_loaded(tmp_path):
    save(tmp_path, make())
    d = _file(tmp_path)
    d["status"] = {"kind": "parked-forever", "detail": {}}
    (tmp_path / "task-portfolio_eval-101.json").write_text(json.dumps(d))
    with pytest.raises(ValueError):
        load(tmp_path, "portfolio_eval", 101)


def test_the_save_refuses_an_occupying_status_with_no_slot(tmp_path):
    with pytest.raises(ValueError):
        save(tmp_path, make(status=login(), slot=NO_SLOT))


@pytest.mark.parametrize("status", [human(), waking(), waking(cause="next-stage"), failed(),
                                    CANCELED, DONE, ci(4), review()])
def test_a_status_that_does_not_occupy_holds_no_slot_after_its_save(tmp_path, status):
    stage = {Kind.PARKED_REVIEW: Stage.PLAN}.get(status.kind, Stage.REVIEW)
    save(tmp_path, make(stage=stage, status=status, slot=2))
    assert load(tmp_path, "portfolio_eval", 101).slot == NO_SLOT


def test_occupies_decides_capacity():
    """capacity=2: two running tasks and one held at a login prompt count as
    three; parked, waking and ended tasks count as none."""
    from dispatcher.claims import box_free
    tasks = [make(issue=1, slot=0), make(issue=2, slot=1),
             make(issue=3, slot=2, status=login()),
             make(issue=4, status=human()), make(issue=5, stage=Stage.REVIEW,
                                                status=waking(cause="next-stage")),
             make(issue=6, status=failed()), make(issue=7, status=CANCELED),
             make(issue=8, status=DONE)]
    assert box_free(2, tasks, triage_running=False) == -1
    assert allocate_slot(tasks, max_slots=4) == 3


def test_terminal_at_is_stamped_at_the_first_ended_status_and_cleared_after(tmp_path):
    save(tmp_path, replace(make(status=failed()), updated_at="2026-07-01T00:00:00+00:00"))
    save(tmp_path, replace(load(tmp_path, "portfolio_eval", 101), status=failed("killed"),
                           updated_at="2026-07-02T00:00:00+00:00"))
    assert load(tmp_path, "portfolio_eval", 101).terminal_at == "2026-07-01T00:00:00+00:00"
    save(tmp_path, make(status=waking(failed(), "resume")))
    assert load(tmp_path, "portfolio_eval", 101).terminal_at == ""


def test_scan_names_a_task_file_it_cannot_read(tmp_path):
    from dispatcher.state import scan
    save(tmp_path, make(issue=1))
    (tmp_path / "task-portfolio_eval-2.json").write_text(json.dumps(
        {"issue": 2, "repository": "portfolio_eval", "stage": "awaiting-plan-review"}))
    tasks, unreadable = scan(tmp_path)
    assert [t.issue for t in tasks] == [1]
    assert [p.name for p in unreadable] == ["task-portfolio_eval-2.json"]
    assert [t.issue for t in load_all(tmp_path)] == [1]


def test_scan_skips_a_task_file_deleted_between_the_glob_and_the_read(tmp_path, monkeypatch):
    from dispatcher import state
    state.save(tmp_path, _ts(1, "agent_ops"))
    state.save(tmp_path, _ts(2, "agent_ops"))
    real_read = state._read

    def read_after_delete(p):
        if p.name == "task-agent_ops-1.json":
            p.unlink()
        return real_read(p)

    monkeypatch.setattr(state, "_read", read_after_delete)
    tasks, unreadable = state.scan(tmp_path)
    assert [t.issue for t in tasks] == [2]
    assert unreadable == []
