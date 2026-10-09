"""Public launch/hook regressions from the independent T2 review."""
import json
import subprocess

import pytest

from dispatcher.runtime_control import RuntimeClient
from tests.test_stop_hook_session_acceptance import hook_fixture, served_hook  # noqa: F401


@pytest.mark.parametrize("metadata", [None, "{", "null", "[]", "{}"])
@pytest.mark.parametrize("ticket", ["", "4"])
def test_complete_launch_environment_survives_missing_or_corrupt_task_metadata(served_hook, metadata, ticket):
    client = RuntimeClient(served_hook.state)
    binding = client.prepare("portfolio_eval", 370, "implement", runtime="claude",
                             ticket=ticket, conversation_id="root",
                             worktree=str(served_hook.worktree))["binding"]
    path = served_hook.worktree / ".agent" / "task.json"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    env = {**served_hook.environment,
           **{f"AGENT_OPS_{key.upper()}": str(binding[key]) for key in
              ("target", "issue", "launch_id", "conversation_id", "stage", "ticket")}}
    for event, expected_status in [("UserPromptSubmit", "active"), ("Stop", "stopped")]:
        result = subprocess.run(["/bin/bash", str(served_hook.hook)], text=True,
                                capture_output=True, timeout=5, env=env,
                                input=json.dumps({"hook_event_name": event,
                                                  "session_id": "root", "prompt_id": "prompt-A",
                                                  "background_tasks": []}))
        assert result.returncode == 0
        view = client.view("portfolio_eval", 370)
        assert view["binding"] == binding
        assert view["main"]["status"] == expected_status
        assert view["main"]["turn_id"] == "prompt-A"
    assert view["inventory"] == "known" and view["workers"] == []


@pytest.fixture
def directory_syncs(monkeypatch):
    import os
    import stat
    from pathlib import Path
    descriptors = {}
    synchronized = []
    failures = set()
    events = []
    real_open, real_fsync, real_mkdir = os.open, os.fsync, os.mkdir

    def open_directory(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        descriptors[fd] = Path(path).resolve() if stat.S_ISDIR(os.fstat(fd).st_mode) else None
        return fd

    def mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        events.append(("mkdir", Path(path).resolve()))
        return result

    def synchronize(fd):
        directory = descriptors.get(fd)
        if directory is not None:
            synchronized.append(directory)
            events.append(("fsync", directory))
            if directory in failures:
                failures.remove(directory)
                raise OSError("injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", open_directory)
    monkeypatch.setattr(os, "fsync", synchronize)
    monkeypatch.setattr(os, "mkdir", mkdir)
    return synchronized, failures, events


def test_acknowledged_first_prepare_syncs_each_new_directory_parent(tmp_path, directory_syncs):
    from dispatcher.runtime_control import RuntimeControl
    from tests.test_bound_turns_t2_acceptance import snapshot_file
    synchronized, _, events = directory_syncs
    state = tmp_path / "new-state"
    control = RuntimeControl(state)
    binding = control.prepare("project", 1, "review")["binding"]
    path = snapshot_file(state, binding)
    # These entries name the new state directory, runtime ownership directory,
    # task ownership directory, and the snapshot/current pointer respectively.
    required = {tmp_path.resolve(), state.resolve(), (state / "runtime").resolve(),
                path.parent.resolve()}
    assert required <= set(synchronized)
    for directory in (state, state / "runtime", path.parent):
        created = events.index(("mkdir", directory.resolve()))
        assert ("fsync", directory.parent.resolve()) in events[created + 1:]
    assert control.view("project", 1)["binding"] == binding


def test_parent_sync_failure_prevents_ack_and_retry_repairs_directory_durability(
        tmp_path, directory_syncs):
    from dispatcher.runtime_control import RuntimeControl
    synchronized, failures, _ = directory_syncs
    state = tmp_path / "new-state"
    failures.add(state.resolve())
    control = RuntimeControl(state)
    with pytest.raises(OSError, match="directory fsync"):
        control.prepare("project", 1, "review")
    synchronized.clear()
    binding = control.prepare("project", 1, "review")["binding"]
    assert state.resolve() in synchronized
    assert (state / "runtime").resolve() in synchronized
    assert control.view("project", 1)["binding"] == binding


@pytest.mark.parametrize("field,records", [
    ("workers", [{}]),
    ("workers", [{"id": [], "status": "running"}]),
    ("workers", [{"id": "", "status": "running"}]),
    ("workers", [{"id": "job", "status": "completed"}]),
    ("workers", [{"id": "job", "status": "unknown-status"}]),
    ("completions", [{}]),
    ("deliveries", [{}]),
    ("alerts", [{}]),
])
def test_unsupported_persisted_records_are_managed_unknown_and_reject_events(tmp_path, field, records):
    from dispatcher.runtime_control import RuntimeControl
    from dispatcher.sessions import Sessions
    from tests.test_bound_turns_t2_acceptance import snapshot_file
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", runtime="claude",
                              conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    assert control.event(binding, {"type": "turn/completed", "thread_id": "root", "turn_id": "A",
                                   "status": "completed", "background_tasks": []})
    path = snapshot_file(tmp_path, binding)
    damaged = json.loads(path.read_text())
    damaged[field] = records
    path.write_text(json.dumps(damaged))
    view = Sessions(state_dir=tmp_path).runtime_view("project", 1)
    assert view is not None and view["main"]["status"] == "unknown"
    assert view["inventory"] == "unknown"
    assert not control.event(binding, {"type": "service", "status": "live"})


def test_valid_native_worker_extra_fields_survive_durable_reopen(tmp_path):
    from dispatcher.runtime_control import RuntimeControl
    worker = {"id": "job", "status": "running", "type": "bash", "command": "sleep 10",
              "description": "Build", "started_at": 100, "extra": {"provider": "native"}}
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", runtime="claude",
                              conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    assert control.event(binding, {"type": "turn/completed", "thread_id": "root", "turn_id": "A",
                                   "status": "completed", "background_tasks": [worker]}, now=100)
    reopened = RuntimeControl(tmp_path).view("project", 1)
    assert reopened["workers"] == [worker]
    assert reopened["main"]["status"] == "stopped" and reopened["inventory"] == "known"


@pytest.mark.parametrize("worker", [{}, {"id": [], "status": "running"},
                                    {"id": "job", "status": "completed"}])
def test_invalid_native_worker_report_persists_unknown_inventory(tmp_path, worker):
    from dispatcher.runtime_control import RuntimeControl
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", runtime="claude",
                              conversation_id="root")["binding"]
    assert control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    assert control.event(binding, {"type": "turn/completed", "thread_id": "root", "turn_id": "A",
                                   "status": "completed", "background_tasks": [worker]})
    reopened = RuntimeControl(tmp_path).view("project", 1)
    assert reopened["inventory"] == "unknown" and reopened["workers"] == []
