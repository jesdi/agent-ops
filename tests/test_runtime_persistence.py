"""Malformed durable/HTTP inputs must never invent stopped evidence."""
import json

import pytest

from dispatcher.runtime_control import RuntimeControl
from tests.runtime_listener import launch_listener  # noqa: F401
from tests.test_stop_hook_session_acceptance import hook_fixture, served_hook  # noqa: F401
from tests.test_bound_turns_t2_acceptance import listener, launch_boundary, native_hooks, post, snapshot_file  # noqa: F401


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10 ** 400, "100", True])
def test_unusable_clock_timestamp_cannot_mutate_launch(tmp_path, value):
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review", runtime="claude",
                              conversation_id="root")["binding"]
    control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    before = control.view("project", 1)
    assert not control.event(binding, {"type": "turn/completed", "thread_id": "root",
                                       "turn_id": "A", "status": "completed",
                                       "background_tasks": [{"id": "job", "status": "running"}]},
                             now=value)
    assert control.view("project", 1) == before


@pytest.mark.parametrize("field,value", [("version", True), ("revision", -1),
                                         ("workers", [None]), ("main", {"status": "stopped"}),
                                         ("main", {"status": "invalid"}),
                                         ("wait", {"since": float("nan"), "workers": []})])
def test_malformed_snapshot_contents_are_managed_unknown(tmp_path, field, value):
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review")["binding"]
    path = snapshot_file(tmp_path, binding)
    snapshot = json.loads(path.read_text())
    snapshot[field] = value
    path.write_text(json.dumps(snapshot))
    view = control.view("project", 1)
    assert view is not None and view["main"]["status"] == "unknown"
    assert not control.event(binding, {"type": "service", "status": "live"})


def test_socket_reader_needs_host_credential_or_current_launch_id(listener):
    binding = RuntimeControl(listener).prepare("project", 1, "review")["binding"]
    status, _ = post(listener, "/runtime/view", {"target": "project", "issue": 1})
    assert status == 403
    status, body = post(listener, "/runtime/view", {"target": "project", "issue": 1,
                                                   "launch_id": "foreign"})
    assert status == 200 and json.loads(body) is None
    status, body = post(listener, "/runtime/view", {"target": "project", "issue": 1,
                                                   "launch_id": binding["launch_id"]})
    assert status == 200 and json.loads(body)["binding"] == binding


@pytest.mark.parametrize("payload", [[], None, {"binding": []}, {"binding": {}, "event": []}])
def test_listener_rejects_malformed_event_without_dying(listener, payload):
    status, _ = post(listener, "/runtime/event", payload)
    assert status in (200, 400)
    status, _ = post(listener, "/runtime/view", {"target": "project", "issue": 1})
    assert status == 403


def test_reappearing_work_resets_clock_after_subset_report(tmp_path):
    from tests.test_bound_turns_t2_acceptance import prepare, start, stop, worker, clock, TARGET, ISSUE
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding, turn="A")
    stop(control, binding, turn="A", work=[worker("one"), worker("two")], now=100)
    start(control, binding, turn="B")
    stop(control, binding, turn="B", work=[worker("two")], now=200)
    start(control, binding, turn="C")
    stop(control, binding, turn="C", work=[worker("one"), worker("two")], now=300)
    assert clock(control.view(TARGET, ISSUE)) == 300


def test_native_stop_records_bound_stage_before_task_transition_is_saved(native_hooks, listener):
    from dispatcher.state import TaskState, Stage, save, read_session
    control, binding, emit, worktree, _ = native_hooks
    save(listener, TaskState(target=binding["target"], issue=binding["issue"],
                             stage=Stage.PLAN, worktree=str(worktree), slot=1,
                             branch="agent/task-370", title="Test", updated_at=""))
    emit("UserPromptSubmit")
    emit("Stop")
    assert read_session(listener, binding["target"], binding["issue"]).stage == "implement"


@pytest.mark.parametrize("native", ["SessionStart", "UserPromptSubmit", "Stop", "SubagentStop"])
def test_listener_native_lifecycle_at_public_ping_boundary(tmp_path, native):
    from dispatcher.waitd import handle_ping
    from dispatcher.state import read_session
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "implement", runtime="claude",
                              conversation_id="root", ticket="4")["binding"]
    payload = {**binding, "session_id": "root", "prompt_id": "A",
               "hook_event_name": native, "background_tasks": []}
    if native == "Stop":
        control.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    before = control.view("project", 1)
    accepted = handle_ping(json.dumps(payload).encode(), tmp_path)
    view = control.view("project", 1)
    if native == "SessionStart":
        assert accepted and view["service"] == "live"
    elif native == "UserPromptSubmit":
        assert accepted and view["main"]["status"] == "active"
    elif native == "Stop":
        assert accepted and view["main"]["status"] == "stopped"
        assert read_session(tmp_path, "project", 1).stage == "implement"
    else:
        assert not accepted and view == before


def test_host_transport_and_socket_authorization_use_served_routes(tmp_path, launch_listener):
    from dispatcher.runtime_http import RuntimeClient, UnixHTTP
    client = RuntimeClient(tmp_path)
    binding = client.prepare("project", 1, "review", conversation_id="root")["binding"]
    assert client.event(binding, {"type": "turn/started", "thread_id": "root", "turn_id": "A"})
    assert client.view("project", 1)["main"]["status"] == "active"
    for route, payload, expected in [
        ("/runtime/prepare", {"target": "project", "issue": 1, "stage": "review"}, 403),
        ("/runtime/view", {"target": "project", "issue": 1}, 403),
        ("/runtime/view", {"target": "project", "issue": 1, "launch_id": binding["launch_id"]}, 200),
        ("/missing", {}, 404),
    ]:
        connection = UnixHTTP(tmp_path)
        try:
            connection.request("POST", route, json.dumps(payload))
            response = connection.getresponse()
            assert response.status == expected
            response.read()
        finally:
            connection.close()


@pytest.mark.parametrize("native", ["SessionStart", "UserPromptSubmit"])
def test_unmanaged_native_input_hook_cannot_create_legacy_waiting(served_hook, native):
    import subprocess
    from dispatcher.state import has_waiting, read_session
    from tests.test_stop_hook_session_acceptance import TARGET, ISSUE, CLAUDE_ID
    result = subprocess.run(["/bin/bash", str(served_hook.hook)],
                            input=json.dumps({"hook_event_name": native, "session_id": CLAUDE_ID,
                                              "prompt_id": "A"}), text=True,
                            env=served_hook.environment, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert not has_waiting(served_hook.state, TARGET, ISSUE)
    assert read_session(served_hook.state, TARGET, ISSUE) is None


@pytest.mark.parametrize("worktree,conversation", [("/other", "root"), ("/worktree", "other")])
def test_resume_cannot_borrow_stage_from_an_unrelated_binding(listener, launch_boundary,
                                                            worktree, conversation):
    from dispatcher.sessions import Sessions
    from dispatcher.runtime_control import RuntimeClient
    RuntimeClient(listener).prepare("project", 1, "implement", ticket="3",
                                    conversation_id="root", worktree="/worktree")
    with pytest.raises(ValueError, match="known task stage"):
        Sessions(state_dir=listener).resume("project", 1, worktree, "Go", "claude-opus-5",
                                             session_id=conversation)
    assert launch_boundary["commands"] == []


def test_missing_current_pointer_retains_managed_unknown_ownership(tmp_path):
    from dispatcher.sessions import Sessions
    control = RuntimeControl(tmp_path)
    binding = control.prepare("project", 1, "review")["binding"]
    snapshot = snapshot_file(tmp_path, binding)
    (snapshot.parent / "current.json").unlink()
    assert Sessions(state_dir=tmp_path).runtime_view("untouched", 2) is None
    view = Sessions(state_dir=tmp_path).runtime_view("project", 1)
    assert view is not None and view["main"]["status"] == "unknown"
    assert not control.event(binding, {"type": "service", "status": "live"})
