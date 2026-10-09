"""CRAP regressions through the approved runtime and task-flow seams."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from dispatcher import main
from dispatcher.runtime_control import RuntimeControl
from dispatcher.state import Stage, load
from t5_dispatcher_support import DispatcherCase
from t5_runtime_support import RuntimeCase, checkpoint, command, native_worker


@pytest.mark.parametrize("identity", [None, {},
    {"kind": "terminal", "thread_id": "root-fixture", "initial_item_id": "exec-A"},
    {"kind": "command", "thread_id": "root-fixture", "initial_item_id": ""},
    {"kind": "agent", "thread_id": "child-fixture", "turn_id": ""},
    {"kind": "command", "thread_id": "root-fixture", "initial_item_id": "exec-A", "turn_id": "A"},
])
def test_malformed_owned_identity_cannot_supply_known_inventory_or_release_session(tmp_path, identity):
    case = RuntimeCase(tmp_path)
    case.stop()
    observation = command(case)
    observation["identity"] = identity
    assert case.inventory([observation])
    view = RuntimeControl(tmp_path).view("fixture", 501)
    assert view["inventory"] == "unknown" and view["workers"] == []
    assert case.retire(reason="stopped") == "held"


@pytest.mark.parametrize("fault", ["null", "foreign-launch", "malformed", "seed-type"])
def test_unverified_checkpoint_does_not_replace_launch_inventory(tmp_path, fault):
    case = RuntimeCase(tmp_path)
    mark = checkpoint(case)
    if fault == "null":
        mark = None
    elif fault == "foreign-launch":
        mark["launch_id"] = "another-launch"
    elif fault == "malformed":
        mark = []
    else:
        mark["seeded"] = "yes"
    before = case.view()
    assert not case.send({"type": "inventory", "certainty": "known", "workers": [],
                          "history_checkpoint": mark})
    assert RuntimeControl(tmp_path).view("fixture", 501) == before


@pytest.mark.parametrize("field,changed", [
    ("seeded", False),
    ("baseline_turns", [{"thread_id": "root-fixture", "turn_id": "old-turn"}]),
    ("baseline_workers", [{"kind": "command", "thread_id": "root-fixture", "initial_item_id": "old-item"}]),
])
def test_seeded_launch_baseline_cannot_change_during_reconnect(tmp_path, field, changed):
    case = RuntimeCase(tmp_path)
    mark = checkpoint(case)
    assert case.inventory([], checkpoint=mark)
    before = case.view()
    mark[field] = changed
    assert not case.inventory([], checkpoint=mark)
    assert RuntimeControl(tmp_path).view("fixture", 501) == before


def test_reconnect_checkpoint_can_advance_scope_without_reseeding_launch(tmp_path):
    case = RuntimeCase(tmp_path)
    mark = checkpoint(case, baseline_turns=[{"thread_id": case.root, "turn_id": "prior-turn"}])
    assert case.inventory([], checkpoint=mark)
    advanced = deepcopy(mark)
    advanced["scopes"] = [{"thread_id": case.root, "turn_id": None,
                           "cursor": "opaque-next-page", "complete": False}]
    assert case.inventory([], checkpoint=advanced)
    reopened = RuntimeControl(tmp_path).view("fixture", 501)
    assert reopened["history_checkpoint"] == advanced
    assert reopened["inventory"] == "known"


def test_legacy_native_wait_reopens_without_resetting_previously_reported_work(tmp_path):
    case = RuntimeCase(tmp_path, runtime="claude")
    assert case.stop(background_tasks=[native_worker("old-job")], now=100)
    path, = tmp_path.glob("runtime/*/" + case.binding["launch_id"] + ".json")
    legacy = json.loads(path.read_text())
    del legacy["wait"]["ever_reported"]
    path.write_text(json.dumps(legacy))
    reopened = RuntimeControl(tmp_path)
    assert reopened.view("fixture", 501)["wait"] == {"since": 100, "workers": ["old-job"]}
    assert reopened.event(case.binding, {"type": "turn/started", "thread_id": case.root, "turn_id": "turn-B"})
    assert reopened.event(case.binding, {"type": "turn/completed", "thread_id": case.root,
        "turn_id": "turn-B", "status": "completed", "background_tasks": [native_worker("old-job")]}, now=900)
    assert reopened.view("fixture", 501)["wait"] == {
        "since": 100, "workers": ["old-job"], "ever_reported": ["old-job"]}
    assert reopened.retire(case.binding, reopened.view("fixture", 501)["revision"],
                           reason="background", now=10900, cap=10800) == "held"


@pytest.mark.parametrize("reported", ["old-job", [""], [None], [{"kind": "terminal", "thread_id": "root"}]])
def test_corrupt_wait_identity_has_no_authority_to_retire_launch(tmp_path, reported):
    case = RuntimeCase(tmp_path, runtime="claude")
    assert case.stop(background_tasks=[native_worker("old-job")])
    path, = tmp_path.glob("runtime/*/" + case.binding["launch_id"] + ".json")
    damaged = json.loads(path.read_text())
    damaged["wait"]["ever_reported"] = reported
    path.write_text(json.dumps(damaged))
    reopened = RuntimeControl(tmp_path)
    assert reopened.view("fixture", 501)["main"]["status"] == "unknown"
    assert reopened.retire(case.binding, damaged["revision"], reason="background", now=50000) == "unknown"


def test_dry_run_cannot_retire_or_close_stopped_launch(monkeypatch):
    with DispatcherCase(monkeypatch) as case:
        assert case.runtime.stop()
        before = case.runtime.view()
        # Keep the retained launch evidence while taking the fixture endpoint
        # offline; Unix sockets cannot be copied into the dry-run state directory.
        (case.state_dir / "wait" / "wait.sock").unlink()
        main.run_pass(case.cfg, case.deps, dry_run=True)
        assert load(case.state_dir, "fixture", 501).stage is Stage.REVIEW
        assert load(case.state_dir, "fixture", 501).park == ""
        assert load(case.state_dir, "fixture", 501).slot == 0
        assert RuntimeControl(case.state_dir).view("fixture", 501) == before
        assert case.tab.alive and not case.physical_ends()
        assert case.notifier.effects == []


def test_listener_transport_loss_before_retirement_holds_stopped_launch(monkeypatch):
    with DispatcherCase(monkeypatch) as case:
        assert case.runtime.stop()
        before = case.runtime.view()
        read = Path.read_text
        snapshot_path, = case.state_dir.glob("runtime/*/" + before["binding"]["launch_id"] + ".json")

        def lose_listener_after_read(path, *args, **kwargs):
            received = read(path, *args, **kwargs)
            if path == snapshot_path:
                (case.state_dir / "wait" / "wait.sock").unlink(missing_ok=True)
            return received

        # Fail the real Unix transport after read-only evidence arrived,
        # leaving conditional retirement unable to acquire listener authority.
        with monkeypatch.context() as fault:
            fault.setattr(Path, "read_text", lose_listener_after_read)
            task = case.run()
        assert task.stage is Stage.REVIEW and task.park == ""
        assert task.slot == 0
        assert case.tab.alive and not case.physical_ends()
        reopened = RuntimeControl(case.state_dir).view("fixture", 501)
        assert reopened == before
