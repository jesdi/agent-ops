"""Locked T2 acceptance, using public snapshots, sessions, HTTP and native hooks.

HTTP contract: POST /runtime/prepare accepts prepare keyword arguments as JSON;
POST /runtime/event accepts {binding, event, now?}; POST /runtime/view accepts
{target, issue, launch_id?}. Host preparation requires a listener credential kept
outside state_dir/wait; possession of the mounted socket alone is insufficient.
RuntimeClient supplies that credential. A successful event response represents
the same bool returned by RuntimeControl.event. Native hook pings retain native
hook_event_name/session_id/prompt_id/agent_id/background_tasks and immutable
AGENT_OPS_LAUNCH_ID/CONVERSATION_ID/STAGE/TICKET launch environment values.

No private runtime or session implementation is used to establish expected state.
External herdr calls are fake; the listener, snapshots and generated hooks are real.
"""

import copy
import http.client
import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID

import pytest

from dispatcher import main, sessions, waitd, workspace
from dispatcher.runtime_control import RuntimeClient, RuntimeControl
from dispatcher.state import PARK_HUMAN, SessionRecord, Stage, load, mark_waiting, write_session
from tests.test_main import FakeSessions, cfg, deps, make_task, patch_usage


TARGET = "portfolio_eval"
ISSUE = 370
CONVERSATION = "cde1213e-113f-4cfe-9846-8a11eba0102f"
PROMPT_A = "eb147afc-f005-4e51-8435-3a8499e32895"
PROMPT_B = "70bff16c-c1c1-4a11-9baa-3aec647d550a"
LISTENERS = {}


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def post(state_dir, route, body):
    conn = UnixHTTP(waitd.sock_path(state_dir))
    try:
        conn.request("POST", route, json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.fixture
def listener():
    # Short socket paths also work on macOS, whose sockaddr_un is only 104 bytes.
    with tempfile.TemporaryDirectory(prefix="t2-", dir="/tmp") as temp:
        state_dir = Path(temp) / "state"
        state_dir.mkdir()
        start_listener(state_dir)
        try:
            yield state_dir
        finally:
            process = LISTENERS.pop(str(state_dir))
            process.terminate()
            process.join(timeout=3)


def start_listener(state_dir):
    process = multiprocessing.get_context("fork").Process(
        target=waitd.serve, args=(waitd.sock_path(state_dir), state_dir), daemon=True)
    process.start()
    LISTENERS[str(state_dir)] = process
    deadline = time.monotonic() + 3
    while not waitd.sock_path(state_dir).exists() and time.monotonic() < deadline:
        assert process.is_alive(), "served wait listener exited before creating its socket"
        time.sleep(0.01)
    assert waitd.sock_path(state_dir).exists(), "served wait listener did not create its socket"


def restart_listener(state_dir):
    process = LISTENERS[str(state_dir)]
    process.terminate()
    process.join(timeout=3)
    waitd.sock_path(state_dir).unlink(missing_ok=True)
    start_listener(state_dir)


def prepare(control, *, stage="implement", ticket="03-api", runtime="claude",
            conversation=CONVERSATION, worktree="/workspace/task-370"):
    return control.prepare(TARGET, ISSUE, stage, ticket=ticket, runtime=runtime,
                           conversation_id=conversation, worktree=worktree)


def start(control, binding, turn=PROMPT_A, now=10):
    assert control.event(binding, {"type": "turn/started",
                                  "thread_id": binding["conversation_id"], "turn_id": turn},
                         now=now), "bound native prompt did not start its main turn"


def stop(control, binding, turn=PROMPT_A, work=(), now=100):
    assert control.event(binding, {"type": "turn/completed", "status": "completed",
                                  "thread_id": binding["conversation_id"], "turn_id": turn,
                                  "background_tasks": list(work)}, now=now), (
                                      "bound normal Stop did not end its current main turn")


def worker(identity):
    return {"id": identity, "type": "shell", "status": "running", "command": "sleep 100"}


def snapshot_file(state_dir, binding):
    found = []
    for path in Path(state_dir).rglob("*"):
        if not path.is_file() or path.is_relative_to(Path(state_dir) / "wait"):
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            continue
        if isinstance(data, dict) and data.get("binding", {}).get("launch_id") == binding["launch_id"]:
            found.append(path)
    assert len(found) == 1, "one durable runtime snapshot must exist outside the mounted wait directory"
    return found[0]


def unknown(view):
    return view is None or (view.get("main", {}).get("status") == "unknown"
                            and view.get("inventory") == "unknown")


def clock(view):
    return (view.get("wait") or {}).get("since")


def test_snapshot_reopens_complete_launch_and_clock_after_restart(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    stop(control, binding, work=[worker("A")], now=100)
    before = control.view(TARGET, ISSUE, binding["launch_id"])
    after = RuntimeControl(tmp_path).view(TARGET, ISSUE, binding["launch_id"])
    assert after == before, "listener restart must preserve the complete durable launch"
    assert after["version"] == 1
    assert clock(after) == 100, "first stopped background report must persist its clock"
    assert set(after) >= {"binding", "revision", "service", "main", "inventory", "workers",
                          "completions", "deliveries", "wait", "alerts", "retired"}


def test_snapshot_updates_use_atomic_replace_and_fsync(tmp_path, monkeypatch):
    replace_calls, fsync_calls = [], []
    real_replace, real_fsync = os.replace, os.fsync

    def replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    path = snapshot_file(tmp_path, binding)
    replace_calls.clear()
    fsync_calls.clear()
    start(control, binding)
    assert any(destination == path and source != path for source, destination in replace_calls), (
        "each persisted event must replace the complete snapshot atomically")
    assert fsync_calls, "atomic snapshot replacement must also be fsynced"


def test_snapshot_is_always_a_complete_json_document_during_updates(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    path = snapshot_file(tmp_path, binding)
    errors, observed = [], []
    finished = threading.Event()

    def observe():
        while not finished.is_set():
            try:
                data = json.loads(path.read_text())
                assert data["version"] == 1 and data["binding"] == binding
                assert {"main", "inventory", "wait", "revision"} <= data.keys()
                observed.append(data["revision"])
            except Exception as error:
                errors.append(error)
                return

    reader = threading.Thread(target=observe)
    reader.start()
    try:
        for n in range(20):
            start(control, binding, turn=f"prompt-{n}")
            stop(control, binding, turn=f"prompt-{n}")
    finally:
        finished.set()
        reader.join(timeout=3)
    assert observed, "concurrent reader must actually observe runtime snapshots"
    assert not errors, f"reader observed a partial runtime snapshot: {errors}"


@pytest.mark.parametrize("damage", ["truncated", "version", "binding", "main", "inventory",
                                     "wait", "revision", "retired", "workers", "deliveries",
                                     "completions", "alerts", "service", "binding.launch_id",
                                     "binding.target", "binding.issue", "binding.stage",
                                     "binding.ticket", "binding.runtime", "binding.worktree",
                                     "binding.conversation_id"])
def test_corrupt_or_incomplete_snapshot_is_unknown_and_rejects_events(tmp_path, damage):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    stop(control, binding)
    path = snapshot_file(tmp_path, binding)
    data = json.loads(path.read_text())
    if damage == "truncated":
        path.write_text('{"version":1,')
    else:
        if damage == "version":
            data["version"] = 999
        elif damage.startswith("binding."):
            del data["binding"][damage.split(".", 1)[1]]
        else:
            del data[damage]
        path.write_text(json.dumps(data))
    restarted = RuntimeControl(tmp_path)
    assert unknown(restarted.view(TARGET, ISSUE)), f"{damage} snapshot must not imply a current Stop"
    assert not restarted.event(binding, {"type": "turn/started", "thread_id": CONVERSATION,
                                         "turn_id": PROMPT_B}), "damaged state cannot be revived by input"


@pytest.mark.parametrize("damage", ["truncated", "missing-main", "version"])
def test_managed_unknown_snapshot_cannot_revive_a_legacy_waiting_park(tmp_path, monkeypatch, damage):
    c = cfg(tmp_path)
    make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    control = RuntimeControl(c.state_dir)
    binding = prepare(control)["binding"]
    path = snapshot_file(c.state_dir, binding)
    if damage == "truncated":
        path.write_text("broken")
    else:
        data = json.loads(path.read_text())
        if damage == "missing-main":
            del data["main"]
        else:
            data["version"] = 999
        path.write_text(json.dumps(data))
    mark_waiting(c.state_dir, TARGET, ISSUE)
    real = sessions.Sessions(state_dir=c.state_dir)
    view = real.runtime_view(TARGET, ISSUE)
    assert view is not None and unknown(view), "Sessions must preserve managed unknown ownership"
    fake = FakeSessions(alive={ISSUE})
    fake.runtime_view = real.runtime_view
    patch_usage(monkeypatch)
    main.run_pass(c, deps(sess=fake))
    assert load(c.state_dir, TARGET, ISSUE).stage is Stage.IMPLEMENT
    assert fake.ended == [], "legacy waiting marker cannot park a corrupt managed launch"


@pytest.mark.parametrize("field,value", [("target", "other"), ("issue", 384),
                                         ("stage", "review"), ("ticket", "04-ui"),
                                         ("runtime", "codex"), ("worktree", "/other"),
                                         ("conversation_id", "foreign"), ("launch_id", "foreign")])
def test_full_launch_binding_is_required_for_every_mutation(tmp_path, field, value):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    before = control.view(TARGET, ISSUE)
    foreign = dict(binding, **{field: value})
    assert not control.event(foreign, {"type": "turn/completed", "thread_id": CONVERSATION,
                                      "turn_id": PROMPT_A, "status": "completed",
                                      "background_tasks": [worker("foreign")]}, now=900)
    assert control.view(TARGET, ISSUE) == before, "foreign stage/ticket/task launch altered state"


def test_new_preparation_fences_old_launch_even_when_conversation_is_reused(tmp_path):
    control = RuntimeControl(tmp_path)
    first = prepare(control)["binding"]
    start(control, first)
    stop(control, first, work=[worker("A")])
    second = prepare(control)["binding"]
    assert UUID(second["launch_id"]) != UUID(first["launch_id"])
    assert second["conversation_id"] == first["conversation_id"]
    assert unknown(control.view(TARGET, ISSUE, first["launch_id"]))
    before = control.view(TARGET, ISSUE)
    assert not control.event(first, {"type": "turn/completed", "thread_id": CONVERSATION,
                                    "turn_id": PROMPT_A, "status": "completed",
                                    "background_tasks": [worker("late")]}, now=500)
    assert control.view(TARGET, ISSUE) == before


@pytest.mark.parametrize("reported", [[worker("A"), worker("B")], [worker("B")]])
def test_same_or_subset_background_report_preserves_original_clock(tmp_path, reported):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    stop(control, binding, work=[worker("A"), worker("B")], now=100)
    start(control, binding, turn=PROMPT_B, now=250)
    stop(control, binding, turn=PROMPT_B, work=reported, now=300)
    assert clock(control.view(TARGET, ISSUE)) == 100


def test_prompt_input_clears_old_stop_but_preserves_background_clock(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    stop(control, binding, work=[worker("A")], now=100)
    start(control, binding, turn=PROMPT_B, now=300)
    view = control.view(TARGET, ISSUE)
    assert view["main"]["status"] == "active"
    assert view["main"]["turn_id"] == PROMPT_B
    assert clock(view) == 100, "foreground input cannot restart the background cap"
    before = copy.deepcopy(view)
    assert not control.event(binding, {"type": "turn/completed", "thread_id": CONVERSATION,
                                      "turn_id": PROMPT_A, "status": "completed",
                                      "background_tasks": [worker("late")]}, now=500)
    assert control.view(TARGET, ISSUE) == before


def test_first_stopped_background_report_starts_clock_and_new_work_resets_it(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    assert clock(control.view(TARGET, ISSUE)) is None
    stop(control, binding, work=[worker("A")], now=100)
    assert clock(control.view(TARGET, ISSUE)) == 100
    start(control, binding, turn=PROMPT_B, now=200)
    assert clock(control.view(TARGET, ISSUE)) == 100
    stop(control, binding, turn=PROMPT_B, work=[worker("A"), worker("C")], now=300)
    assert clock(control.view(TARGET, ISSUE)) == 300


@pytest.mark.parametrize("inventory", [None, "unreadable", {}])
def test_invalid_native_background_inventory_is_unknown_not_empty(tmp_path, inventory):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)["binding"]
    start(control, binding)
    stop(control, binding)
    assert control.view(TARGET, ISSUE)["inventory"] == "known", (
        "a complete empty native Stop report must establish known inventory")
    start(control, binding, turn=PROMPT_B)
    event = {"type": "turn/completed", "thread_id": CONVERSATION,
             "turn_id": PROMPT_B, "status": "completed"}
    if inventory is not None:
        event["background_tasks"] = inventory
    control.event(binding, event, now=100)
    assert control.view(TARGET, ISSUE)["inventory"] == "unknown"


def test_runtime_client_prepares_and_mutates_through_served_http(listener, monkeypatch):
    # Patch only this process after listener fork: any direct client-side write fails.
    def forbidden(*args, **kwargs):
        pytest.fail("RuntimeClient bypassed HTTP and mutated RuntimeControl in the client process")

    monkeypatch.setattr(RuntimeControl, "prepare", forbidden)
    monkeypatch.setattr(RuntimeControl, "event", forbidden)
    client = RuntimeClient(listener)
    binding = prepare(client)["binding"]
    start(client, binding)
    stop(client, binding, work=[worker("A")], now=100)
    view = client.view(TARGET, ISSUE, binding["launch_id"])
    assert view["main"]["status"] == "stopped"
    assert clock(view) == 100
    path = snapshot_file(listener, binding)
    assert not path.is_relative_to(listener / "wait")


def test_listener_restart_preserves_http_launch_and_wait_clock(listener):
    client = RuntimeClient(listener)
    binding = prepare(client)["binding"]
    start(client, binding)
    stop(client, binding, work=[worker("A")], now=100)
    before = client.view(TARGET, ISSUE)
    restart_listener(listener)
    after = RuntimeClient(listener).view(TARGET, ISSUE)
    assert after == before, "new served listener must reopen complete launch state"
    assert clock(after) == 100


def test_socket_possession_does_not_authorize_host_preparation(listener):
    before = {str(p.relative_to(listener)): p.read_bytes() for p in listener.rglob("*")
              if p.is_file()}
    status, _ = post(listener, "/runtime/prepare", {"target": TARGET, "issue": ISSUE,
                                                   "stage": "implement", "ticket": "03-api",
                                                   "runtime": "claude",
                                                   "conversation_id": CONVERSATION})
    assert status in (401, 403), "mounted client must not prepare/rebind physical launches"
    after = {str(p.relative_to(listener)): p.read_bytes() for p in listener.rglob("*")
             if p.is_file()}
    assert after == before, "unauthenticated preparation wrote listener state"


def test_mounted_client_events_need_exact_binding_but_no_host_prepare_credential(listener):
    control = RuntimeControl(listener)
    binding = prepare(control)["binding"]
    event = {"type": "turn/started", "thread_id": CONVERSATION, "turn_id": PROMPT_A}
    status, _ = post(listener, "/runtime/event", {"binding": binding, "event": event, "now": 10})
    assert status == 200, "prepared container must be able to submit its own launch-bound events"
    assert control.view(TARGET, ISSUE)["main"]["status"] == "active", (
        "served HTTP route must apply its valid bound event")
    before = control.view(TARGET, ISSUE)
    foreign = dict(binding, ticket="04-ui")
    post(listener, "/runtime/event", {"binding": foreign, "event": {
        "type": "turn/completed", "thread_id": CONVERSATION, "turn_id": PROMPT_A,
        "status": "completed", "background_tasks": [worker("late")]}, "now": 500})
    assert control.view(TARGET, ISSUE) == before


@pytest.fixture
def launch_boundary(monkeypatch):
    calls = {"env": [], "commands": [], "provider": []}
    monkeypatch.setattr(sessions.herdr, "tab",
                        lambda *args: ("workspace", "tab") if calls["env"] else None)
    monkeypatch.setattr(sessions.herdr, "ensure_workspace", lambda *args: "workspace")

    def tab(*args, **kwargs):
        calls["env"].append(kwargs.get("env") or (args[3] if len(args) > 3 else {}))
        return "tab"

    monkeypatch.setattr(sessions.herdr, "create_tab", tab)
    monkeypatch.setattr(sessions.herdr, "root_pane", lambda *args: "pane")
    monkeypatch.setattr(sessions.herdr, "run_command",
                        lambda pane, command: calls["commands"].append(command) or True)

    def command(*args, **kwargs):
        calls["provider"].append((args, kwargs))
        return "podman-run-test"

    monkeypatch.setattr(sessions.containers, "session_cmd", command)
    return calls


def launch_env(calls):
    env = {}
    for record in calls["env"]:
        env.update(record)
    for command in calls["commands"]:
        for word in shlex.split(command):
            if word.startswith("AGENT_OPS_") and "=" in word:
                key, value = word.split("=", 1)
                env[key] = value
    for _, kwargs in calls["provider"]:
        for key in ("env", "launch_env", "extra_env"):
            if isinstance(kwargs.get(key), dict):
                env.update(kwargs[key])
    return env


def test_sessions_spawn_binds_explicit_stage_ticket_conversation_and_launch_env(
        listener, tmp_path, launch_boundary, monkeypatch):
    wt = tmp_path / "worktree"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "task.json").write_text(json.dumps({"target": TARGET, "issue": ISSUE}))
    real = sessions.Sessions(state_dir=listener)

    def forbidden(*args, **kwargs):
        pytest.fail("Sessions prepared a launch without the listener HTTP client")

    monkeypatch.setattr(RuntimeControl, "prepare", forbidden)
    real.spawn_stage(TARGET, ISSUE, str(wt), "Implement ticket", "implement",
                     "anthropic/claude-opus-5", ticket="03-api")
    view = real.runtime_view(TARGET, ISSUE)
    assert view is not None
    binding = view["binding"]
    assert binding["stage"] == "implement" and binding["ticket"] == "03-api"
    assert binding["runtime"] == "claude" and binding["worktree"] == str(wt)
    UUID(binding["launch_id"])
    UUID(binding["conversation_id"])
    env = launch_env(launch_boundary)
    assert env["AGENT_OPS_LAUNCH_ID"] == binding["launch_id"]
    assert env["AGENT_OPS_CONVERSATION_ID"] == binding["conversation_id"]
    assert env["AGENT_OPS_STAGE"] == "implement" and env["AGENT_OPS_TICKET"] == "03-api"
    provider = str(launch_boundary["provider"])
    assert "--session-id" in provider and binding["conversation_id"] in provider, (
        "fresh Claude launch must choose its session before SessionStart arrives")
    snapshot_file(listener, binding)


def test_sessions_resume_gets_fresh_launch_for_same_recorded_conversation(
        listener, tmp_path, launch_boundary):
    c = cfg(tmp_path)
    c = replace(c, state_dir=str(listener))
    wt = make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    first = prepare(RuntimeClient(listener), worktree=str(wt))["binding"]
    write_session(listener, TARGET, ISSUE,
                  SessionRecord(session_id=CONVERSATION, stage="implement"))
    real = sessions.Sessions(state_dir=listener)
    real.resume(TARGET, ISSUE, str(wt), "Continue ticket", "anthropic/claude-opus-5",
                session_id=CONVERSATION)
    resumed = real.runtime_view(TARGET, ISSUE)["binding"]
    assert resumed["conversation_id"] == CONVERSATION
    assert UUID(resumed["launch_id"]) != UUID(first["launch_id"])
    assert resumed["stage"] == first["stage"]
    assert launch_env(launch_boundary)["AGENT_OPS_LAUNCH_ID"] == resumed["launch_id"]
    assert resumed["ticket"] == first["ticket"]
    assert clock(real.runtime_view(TARGET, ISSUE)) is None


@pytest.mark.parametrize("action", ["spawn", "resume"])
def test_real_launch_requires_state_directory(tmp_path, launch_boundary, action):
    real = sessions.Sessions()
    with pytest.raises((ValueError, RuntimeError), match="state_dir"):
        if action == "spawn":
            real.spawn_stage(TARGET, ISSUE, str(tmp_path), "Work", "review", "claude-opus-5")
        else:
            real.resume(TARGET, ISSUE, str(tmp_path), "Continue", "claude-opus-5",
                        session_id=CONVERSATION)
    assert not launch_boundary["commands"], "state-less launch must not start a physical session"


@pytest.mark.parametrize("action", ["spawn", "resume"])
def test_dry_run_launch_creates_no_files(tmp_path, launch_boundary, action):
    state_dir = tmp_path / "state"
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    real = sessions.Sessions(dry_run=True, state_dir=state_dir)
    if action == "spawn":
        real.spawn_stage(TARGET, ISSUE, str(tmp_path / "worktree"), "Work", "implement",
                         "anthropic/claude-opus-5", ticket="03-api")
    else:
        real.resume(TARGET, ISSUE, str(tmp_path / "worktree"), "Continue",
                    "anthropic/claude-opus-5", session_id=CONVERSATION)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before, "dry runs must not prepare snapshots, credentials or hooks"
    assert not state_dir.exists() and not (tmp_path / "worktree").exists()
    assert not launch_boundary["commands"]


@pytest.fixture
def native_hooks(listener, tmp_path):
    wt = tmp_path / "worktree"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "task.json").write_text(json.dumps({"target": TARGET, "issue": ISSUE}))
    workspace.install_stop_hook(str(wt))
    settings = json.loads((wt / ".claude" / "settings.local.json").read_text())
    control = RuntimeControl(listener)
    binding = prepare(control, worktree=str(wt))["binding"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "hook-http.jsonl"
    curl = bin_dir / "curl"
    # Redirect only the container's socket mount to this real served test listener.
    curl.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
args = sys.argv[1:]
for n, arg in enumerate(args):
    if arg == "--unix-socket":
        args[n + 1] = os.environ["T2_WAIT_SOCKET"]
payload = None
for n, arg in enumerate(args):
    if arg in ("-d", "--data", "--data-raw", "--data-binary"):
        payload = args[n + 1]
        break
if payload in ("@-", "-"):
    payload = sys.stdin.read()
    args[n + 1] = payload
elif payload and payload.startswith("@"):
    payload = open(payload[1:]).read()
    args[n + 1] = payload
with open(os.environ["T2_CAPTURE"], "a") as output:
    output.write(json.dumps({"args": args, "payload": payload}) + "\\n")
sys.exit(subprocess.call(["/usr/bin/curl", *args]))
''')
    curl.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "CLAUDE_PROJECT_DIR": str(wt), "AGENT_OPS_LAUNCH_ID": binding["launch_id"],
           "AGENT_OPS_CONVERSATION_ID": CONVERSATION, "AGENT_OPS_STAGE": "implement",
           "AGENT_OPS_TICKET": "03-api", "T2_WAIT_SOCKET": str(waitd.sock_path(listener)),
           "T2_CAPTURE": str(capture)}

    def emit(event_name, *, session=CONVERSATION, prompt=PROMPT_A, **extra):
        entries = settings.get("hooks", {}).get(event_name, [])
        assert entries, f"native {event_name} hook must be installed"
        payload = {"hook_event_name": event_name, "session_id": session,
                   "cwd": str(wt), "transcript_path": str(wt / "native.jsonl"), **extra}
        if event_name != "SessionStart":
            payload["prompt_id"] = prompt
        else:
            payload["source"] = "startup"
        if event_name == "UserPromptSubmit":
            payload["prompt"] = "Native prompt"
        if event_name == "Stop":
            payload.setdefault("background_tasks", [])
            payload["stop_hook_active"] = False
            payload["last_assistant_message"] = "ISOLATED_CLAUDE_DONE"
            payload["session_crons"] = []
        if event_name in ("Stop", "UserPromptSubmit"):
            payload["permission_mode"] = "bypassPermissions"
        for entry in entries:
            for hook in entry["hooks"]:
                result = subprocess.run(["/bin/bash", "-c", hook["command"]], cwd=wt,
                                        env=env, input=json.dumps(payload), text=True,
                                        capture_output=True, timeout=5)
                assert result.returncode == 0, f"native {event_name} hook failed: {result.stderr}"
        return payload

    return control, binding, emit, wt, capture


def test_native_session_start_only_establishes_selected_session_health(native_hooks):
    control, binding, emit, _, _ = native_hooks
    before = control.view(TARGET, ISSUE)
    emit("SessionStart", session="foreign-session")
    assert control.view(TARGET, ISSUE) == before
    emit("SessionStart")
    view = control.view(TARGET, ISSUE)
    assert view["binding"] == binding
    assert view["service"] == "live"
    assert view["main"]["status"] == "unknown", "SessionStart alone cannot establish a Stop"


@pytest.mark.parametrize("foreign", [{"agent_id": "child-agent"}, {"session": "foreign-session"},
                                    {"prompt": None}])
def test_native_foreign_child_or_unidentified_prompt_cannot_start_main_turn(native_hooks, foreign):
    control, _, emit, _, _ = native_hooks
    before = control.view(TARGET, ISSUE)
    emit("UserPromptSubmit", **foreign)
    assert control.view(TARGET, ISSUE) == before
    emit("UserPromptSubmit")
    assert control.view(TARGET, ISSUE)["main"]["status"] == "active"


def test_native_prompt_stop_and_new_prompt_use_exact_ids_and_preserve_clock(native_hooks):
    control, _, emit, _, _ = native_hooks
    emit("UserPromptSubmit")
    assert control.view(TARGET, ISSUE)["main"]["turn_id"] == PROMPT_A
    emit("Stop", background_tasks=[worker("A")])
    stopped = control.view(TARGET, ISSUE)
    assert stopped["main"]["status"] == "stopped"
    assert clock(stopped) is not None
    emit("UserPromptSubmit", prompt=PROMPT_B)
    active = control.view(TARGET, ISSUE)
    assert active["main"]["status"] == "active" and active["main"]["turn_id"] == PROMPT_B
    assert clock(active) == clock(stopped)
    emit("Stop", prompt=PROMPT_A, background_tasks=[worker("late")])
    assert control.view(TARGET, ISSUE) == active


@pytest.mark.parametrize("foreign", [{"agent_id": "child-agent"}, {"session": "foreign-session"},
                                    {"prompt": "old-prompt"}])
def test_native_child_foreign_or_old_stop_cannot_end_main_turn(native_hooks, foreign):
    control, binding, emit, _, _ = native_hooks
    start(control, binding)
    before = control.view(TARGET, ISSUE)
    emit("Stop", **foreign)
    assert control.view(TARGET, ISSUE) == before
    emit("Stop")
    assert control.view(TARGET, ISSUE)["main"]["status"] == "stopped", (
        "rejection evidence requires a functioning bound main Stop hook")


def test_native_hook_identity_is_launch_environment_not_mutable_worktree_file(native_hooks):
    control, binding, emit, wt, capture = native_hooks
    start(control, binding)
    # A replacement's worktree metadata cannot impersonate the old hook process.
    (wt / ".agent" / "runtime-binding.json").write_text(json.dumps(
        dict(binding, launch_id="replacement", conversation_id="foreign", ticket="04-ui")))
    (wt / ".agent" / "task.json").write_text(json.dumps(
        dict(binding, launch_id="replacement", conversation_id="foreign", ticket="04-ui")))
    emit("Stop")
    assert control.view(TARGET, ISSUE)["main"]["status"] == "stopped"
    assert capture.exists(), "installed hook must forward its native payload over HTTP"
    records = [json.loads(line) for line in capture.read_text().splitlines()]
    payloads = [json.loads(record["payload"]) for record in records if record["payload"]]
    assert payloads, "native Stop must emit a JSON launch-bound request"
    outgoing = payloads[-1]
    assert outgoing["launch_id"] == binding["launch_id"]
    assert outgoing["session_id"] == CONVERSATION and outgoing["prompt_id"] == PROMPT_A
    assert outgoing["hook_event_name"] == "Stop"
    assert outgoing["stage"] == "implement" and outgoing["ticket"] == "03-api"
    assert outgoing["background_tasks"] == []


def test_old_native_hook_process_cannot_change_replacement_same_conversation(native_hooks):
    control, first, emit, _, _ = native_hooks
    start(control, first)
    emit("Stop")
    assert control.view(TARGET, ISSUE)["main"]["status"] == "stopped", (
        "prior-launch rejection requires a functioning bound main Stop hook")
    replacement = prepare(control)["binding"]
    start(control, replacement, turn=PROMPT_B)
    before = control.view(TARGET, ISSUE)
    emit("Stop", prompt=PROMPT_B, background_tasks=[worker("late")])
    assert control.view(TARGET, ISSUE) == before, "old immutable launch env must be rejected"


@pytest.mark.parametrize("signal", ["blocked", "awaiting-answers"])
def test_stage_gates_keep_authority_with_managed_unknown_runtime(tmp_path, monkeypatch, signal):
    c = cfg(tmp_path)
    wt = make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    (wt / "answers.md").write_text("# Operator answers\n")
    (wt / ".agent" / "stage.json").write_text(json.dumps(
        {"stage": "implement", "status": signal, "reason": "operator needed",
         "artifact": "answers.md"}))
    prepare(RuntimeControl(c.state_dir))
    real = sessions.Sessions(state_dir=c.state_dir)
    fake = FakeSessions(alive={ISSUE})
    fake.runtime_view = real.runtime_view
    patch_usage(monkeypatch)
    main.run_pass(c, deps(sess=fake))
    task = load(c.state_dir, TARGET, ISSUE)
    assert task.stage is Stage.IMPLEMENT and task.park == PARK_HUMAN
    assert fake.ended == [ISSUE]


def test_dead_session_remains_crash_despite_managed_unknown_snapshot(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    make_task(c, issue=ISSUE, stage=Stage.IMPLEMENT)
    prepare(RuntimeControl(c.state_dir))
    real = sessions.Sessions(state_dir=c.state_dir)
    fake = FakeSessions(alive=set())
    fake.runtime_view = real.runtime_view
    patch_usage(monkeypatch)
    main.run_pass(c, deps(sess=fake))
    assert load(c.state_dir, TARGET, ISSUE).stage is Stage.FAILED
