"""Locked T4 black-box acceptance against declared launch and RPC contracts."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from dispatcher.runtime_control import RuntimeControl
from dispatcher.runtime_http import BoundClient, RuntimeClient
from dispatcher import workspace


REPO = Path(__file__).resolve().parents[1]
TARGET, ISSUE, ROOT = "t4-target", 384, "root"


def eventually(predicate, message, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail(message)


def operation(client, name):
    value = getattr(client, name, None)
    assert callable(value), f"public {type(client).__name__}.{name} is required by T4"
    return value


class World:
    def __init__(self, directory):
        self.root = Path(directory)
        self.state = self.root / "state"
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.processes = []
        self.streams = []
        self.listener = None
        self.host = RuntimeClient(self.state)
        self.external = self.root / "external"
        self.external.mkdir()
        self.configure(hold_reads=False, inputs={})

    def spawn(self, args, *, env=None, cwd=None, name="process"):
        stream = (self.root / f"{name}.log").open("w")
        self.streams.append(stream)
        process = subprocess.Popen(args, cwd=cwd or REPO, env=env or self.env(),
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def env(self, binding=None):
        result = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / ".codex"), PYTHONPATH=str(REPO), AGENT_OPS_STATE_DIR=str(self.state))
        if binding:
            result.update(AGENT_OPS_TARGET=binding["target"], AGENT_OPS_ISSUE=str(binding["issue"]),
                          AGENT_OPS_LAUNCH_ID=binding["launch_id"],
                          AGENT_OPS_CONVERSATION_ID=binding.get("conversation_id") or "",
                          AGENT_OPS_STAGE=binding["stage"], AGENT_OPS_TICKET=binding["ticket"])
        return result

    def start_listener(self):
        self.listener = self.spawn([sys.executable, "-m", "dispatcher.waitd"], name="listener")
        def ready():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.connect(str(self.state / "wait" / "wait.sock"))
                return True
            except OSError:
                return False
        eventually(ready, "owned listener did not bind")

    def stop(self, process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=4)

    def restart_listener(self):
        self.stop(self.listener)
        self.start_listener()
        eventually(lambda: self.host.view(TARGET, ISSUE), "listener did not recover its launch")

    def prepare(self, *, runtime="codex", issue=ISSUE, ticket="04-fence", conversation=ROOT, stage="review"):
        return self.host.prepare(TARGET, issue, stage, ticket=ticket, runtime=runtime,
                                 conversation_id=conversation, worktree=str(self.worktree))["binding"]

    def view(self, binding):
        return self.host.view(binding["target"], binding["issue"], binding["launch_id"])

    def stopped(self, binding, turn="turn-A", *, now=100):
        for event in ({"type": "service", "status": "live"},
                      {"type": "turn/started", "thread_id": ROOT, "turn_id": turn, "status": "inProgress"},
                      {"type": "inventory", "certainty": "known", "workers": []},
                      {"type": "turn/completed", "thread_id": ROOT, "turn_id": turn, "status": "completed"}):
            assert self.host.event(binding, event, now=now), f"public stopped setup rejected {event}"
        return self.view(binding)

    def configure(self, **updates):
        path = self.external / "control.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update(updates)
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps(current))
        temporary.replace(path)

    def logs(self, kind=None):
        path = self.external / "wire.jsonl"
        items = []
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # A concurrent append may expose only the final partial line.
        return [item for item in items if kind is None or item["kind"] == kind]

    def command(self, name, action, **fields):
        token = uuid.uuid4().hex
        with (self.external / f"{name}.commands").open("a") as stream:
            stream.write(json.dumps(dict(action=action, token=token, **fields)) + "\n")
        return token

    def terminal(self, packet, *, connection="primary"):
        token = self.command("terminal", "send", connection=connection, packet=packet)
        eventually(lambda: any(item["token"] == token for item in self.logs("terminal_command")),
                   "fake terminal did not send native RPC")

    def input_count(self, client_id):
        return sum(item["client_id"] == client_id for item in self.logs("input_received"))

    def supervisor(self, binding, *, ready=True):
        binary_dir = self.root / "bin"
        binary_dir.mkdir(exist_ok=True)
        binary = binary_dir / "codex"
        binary.write_text(f"#!{sys.executable}\n" + (REPO / "tests/t4_codex_external.py").read_text())
        binary.chmod(0o755)
        prompt = self.root / "prompt.txt"
        prompt.write_text("T4_BOOTSTRAP_INPUT")
        env = self.env(binding)
        env.update(PATH=f"{binary_dir}:{os.environ['PATH']}", T4_EXTERNAL=str(self.external))
        arguments = [sys.executable, "-P", "-m", "dispatcher.codex_supervisor",
                     "--model", "gpt-fixture", "--prompt-file", str(prompt)]
        if binding.get("conversation_id"):
            arguments.extend(["--resume", binding["conversation_id"]])
        process = self.spawn(arguments,
                             env=env, cwd=self.worktree, name="supervisor")
        if ready:
            try:
                eventually(lambda: self.logs("terminal_ready"), "real supervisor did not attach external terminal")
            except BaseException:
                diagnostic = [line for line in (self.root / "supervisor.log").read_text().splitlines()
                              if line and not line.startswith(" ")]
                pytest.fail(f"external terminal fixture not ready: status={process.poll()}, wire={self.logs()}, errors={diagnostic}")
        return process

    def stop_bootstrap(self, binding):
        self.configure(hold_reads=True)
        token = self.command("backend", "complete")
        eventually(lambda: any(item["token"] == token for item in self.logs("backend_command")),
                   "external backend did not complete bootstrap")
        eventually(lambda: self.view(binding)["main"]["status"] == "stopped",
                   "normal bootstrap Stop did not reach listener")
        assert self.host.event(binding, {"type": "inventory", "certainty": "known", "workers": []})
        return self.view(binding)

    def cleanup(self):
        owned_children = {item["pid"] for item in self.logs("fixture_process")}
        for process in reversed(self.processes):
            self.stop(process)
        for pid in owned_children:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        def child_gone(pid):
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        eventually(lambda: all(child_gone(pid) for pid in owned_children),
                   "owned external fixture child did not exit", timeout=4)
        for stream in self.streams:
            stream.close()
        assert all(process.poll() is not None for process in self.processes)


@pytest.fixture
def world(request):
    with tempfile.TemporaryDirectory(prefix="t4-", dir="/tmp") as directory:
        instance = World(directory)
        try:
            instance.start_listener()
            yield instance
        finally:
            instance.cleanup()
            report_dir = Path("/tmp/agent-ops-stage2-t4-intake/fixture-observations")
            report_dir.mkdir(exist_ok=True)
            observed = {"root": str(instance.root), "wire": instance.logs(), "cleaned_processes": [process.returncode for process in instance.processes], "owned_child_cleanup_verified": True}
            for name in ("listener.log", "supervisor.log"):
                path = instance.root / name
                if path.exists():
                    observed[name] = [line for line in path.read_text().splitlines() if line and not line.startswith(" ")]
            from dispatcher.state import load
            state = load(instance.state, TARGET, ISSUE)
            if state:
                observed["task"] = vars(state)
            observed["runtime"] = instance.host.view(TARGET, ISSUE)
            observed["external"] = getattr(instance, "dispatcher_external", [])
            digest = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:12]
            (report_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', request.node.name[:80])}-{digest}.json").write_text(json.dumps(observed, default=str, indent=2))


@pytest.fixture
def direct():
    with tempfile.TemporaryDirectory(prefix="t4-direct-", dir="/tmp") as directory:
        yield RuntimeControl(directory)


def prepare_direct(control, **options):
    return control.prepare(TARGET, ISSUE, "review", ticket="04-fence", conversation_id=ROOT, **options)["binding"]


def stopped_direct(control, binding):
    for event in ({"type": "service", "status": "live"},
                  {"type": "turn/started", "thread_id": ROOT, "turn_id": "turn-A", "status": "inProgress"},
                  {"type": "inventory", "certainty": "known", "workers": []},
                  {"type": "turn/completed", "thread_id": ROOT, "turn_id": "turn-A", "status": "completed"}):
        assert control.event(binding, event, now=100)
    return control.view(TARGET, ISSUE)


def rpc(method, client_id, *, request_id="wire-id", turn="turn-1", thread=ROOT):
    params = {"threadId": thread, "clientUserMessageId": client_id,
              "input": [{"type": "text", "text": client_id}]}
    if method == "turn/steer":
        params["expectedTurnId"] = turn
    return {"id": request_id, "method": method, "params": params}


def test_admission_before_turn_started_holds_old_stop(direct):
    binding = prepare_direct(direct)
    snapshot = stopped_direct(direct, binding)
    assert operation(direct, "accept_input")(binding, "operator-input") is True
    assert operation(direct, "retire")(binding, snapshot["revision"]) != "retired"
    current = direct.view(TARGET, ISSUE)
    assert operation(direct, "retire")(binding, current["revision"]) == "held"
    assert current["retired"] is False


def test_confirmed_retirement_rejects_main_input_and_lifecycle(direct):
    binding = prepare_direct(direct)
    snapshot = stopped_direct(direct, binding)
    assert operation(direct, "retire")(binding, snapshot["revision"]) == "retired"
    retired = direct.view(TARGET, ISSUE)
    assert operation(direct, "accept_input")(binding, "late-input") is False
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "late-turn", "status": "inProgress"}) is False
    assert direct.view(TARGET, ISSUE) == retired


def test_latest_stop_and_stale_revision_cannot_retire_active_turn(direct):
    binding = prepare_direct(direct)
    old = stopped_direct(direct, binding)
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "turn-B", "status": "inProgress"})
    assert direct.event(binding, {"type": "turn/completed", "thread_id": ROOT,
                                  "turn_id": "turn-A", "status": "completed"}) is False
    assert operation(direct, "retire")(binding, old["revision"]) != "retired"
    current = direct.view(TARGET, ISSUE)
    assert current["main"]["status"] == "active"
    assert operation(direct, "retire")(binding, current["revision"]) != "retired"


def test_unknown_control_cannot_authorize_automatic_retirement(direct):
    binding = prepare_direct(direct)
    stopped_direct(direct, binding)
    assert direct.event(binding, {"type": "control/unknown", "message": "fixture visibility unavailable"})
    unknown = direct.view(TARGET, ISSUE)
    assert operation(direct, "retire")(binding, unknown["revision"]) != "retired"
    assert direct.view(TARGET, ISSUE)["retired"] is False


@pytest.mark.parametrize("field,value", [("target", "other"), ("issue", 281), ("stage", "implement"),
    ("ticket", "03-old"), ("launch_id", "foreign-launch"), ("runtime", "claude"),
    ("worktree", "/foreign"), ("conversation_id", "foreign-root")])
def test_exact_binding_required_for_admission_and_forced_retirement(direct, field, value):
    binding = prepare_direct(direct)
    snapshot = stopped_direct(direct, binding)
    foreign = dict(binding, **{field: value})
    assert operation(direct, "accept_input")(foreign, "foreign-input") is False
    assert operation(direct, "retire")(foreign, snapshot["revision"], reason="forced") != "retired"
    assert direct.view(TARGET, ISSUE) == snapshot


def test_claude_input_preserves_populated_wait_clock(direct):
    binding = prepare_direct(direct, runtime="claude")
    assert direct.event(binding, {"type": "service", "status": "live"}, now=100)
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "prompt-A", "status": "inProgress"}, now=100)
    assert direct.event(binding, {"type": "turn/completed", "thread_id": ROOT,
                                  "turn_id": "prompt-A", "status": "completed", "background_tasks": [
        {"id": "native-shell-A", "type": "shell", "status": "running",
         "command": "fixture-command", "description": "fixture work"}]}, now=100)
    before = direct.view(TARGET, ISSUE)
    assert operation(direct, "accept_input")(binding, "foreground-input")
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "prompt-B", "status": "inProgress"}, now=200)
    after = direct.view(TARGET, ISSUE)
    assert after["wait"] == before["wait"]
    assert operation(direct, "retire")(binding, after["revision"]) != "retired"


def test_host_transport_admission_and_retirement_are_durable(world):
    binding = world.prepare()
    snapshot = world.stopped(binding)
    assert operation(world.host, "accept_input")(binding, "durable-input") is True
    world.restart_listener()
    restored = world.view(binding)
    assert operation(world.host, "retire")(binding, restored["revision"]) == "held"
    fresh = world.prepare(ticket="05-next")
    stopped = world.stopped(fresh)
    assert operation(world.host, "retire")(fresh, stopped["revision"]) == "retired"
    world.restart_listener()
    assert world.view(fresh)["retired"] is True
    assert operation(world.host, "accept_input")(fresh, "after-restart") is False
    assert world.host.view(TARGET, ISSUE, snapshot["binding"]["launch_id"]) is None


def test_container_transport_can_admit_only_its_exact_launch(world):
    binding = world.prepare()
    world.stopped(binding)
    bound = BoundClient(world.state, TARGET, ISSUE, binding["launch_id"])
    assert operation(bound, "accept_input")(binding, "container-input") is True
    other = world.prepare(issue=281, conversation="other-root")
    assert operation(bound, "accept_input")(other, "other-task") is False
    replacement = world.prepare(ticket="05-next")
    assert operation(bound, "accept_input")(replacement, "next-ticket") is False
    assert operation(bound, "accept_input")(binding, "old-ticket") is False
    assert world.view(replacement)["retired"] is False


def test_host_only_retirement_rejects_unauthorized_http(world):
    binding = world.prepare()
    snapshot = world.stopped(binding)
    payload = json.dumps({"binding": binding, "revision": snapshot["revision"], "reason": "forced",
                          "now": 100, "cap": 10800})
    for headers in ([], ["-H", "X-Runtime-Host: deliberately-invalid"]):
        result = subprocess.run(["/usr/bin/curl", "--silent", "--unix-socket",
                                 str(world.state / "wait/wait.sock"), "-o", "/dev/null", "-w", "%{http_code}",
                                 "-H", "Content-Type: application/json", *headers, "-d", payload,
                                 "http://localhost/runtime/retire"], capture_output=True, text=True, timeout=3)
        assert result.returncode == 0
        assert result.stdout in {"401", "403"}, "unauthorized runtime retirement must fail authentication"
        assert world.view(binding) == snapshot


def test_concurrent_listener_input_and_retirement_have_one_safe_order(world):
    admit = operation(world.host, "accept_input")
    retire = operation(world.host, "retire")
    for index in range(8):
        binding = world.prepare(ticket=f"04-race-{index}")
        snapshot = world.stopped(binding)
        from threading import Barrier
        barrier = Barrier(2)
        def input_request():
            barrier.wait()
            return admit(binding, f"concurrent-{index}")
        def retirement_request():
            barrier.wait()
            return retire(binding, snapshot["revision"])
        with ThreadPoolExecutor(max_workers=2) as executor:
            input_future = executor.submit(input_request)
            retirement_future = executor.submit(retirement_request)
            accepted, result = input_future.result(), retirement_future.result()
        assert (accepted and result != "retired") or (not accepted and result == "retired")
        assert world.view(binding)["retired"] is (result == "retired")


@pytest.mark.parametrize("method", ["turn/start", "turn/steer"])
def test_retired_gateway_rejects_terminal_native_turn_rpc(world, method):
    binding = world.prepare()
    world.supervisor(binding)
    snapshot = world.stop_bootstrap(binding)
    assert operation(world.host, "retire")(binding, snapshot["revision"]) == "retired"
    packet = rpc(method, f"retired-{method}", request_id="retired-request")
    world.terminal(packet)
    response = eventually(lambda: next((item["packet"] for item in world.logs("terminal_receive")
        if item["packet"].get("id") == "retired-request"), None), "retired gateway did not reject native RPC")
    assert "error" in response
    assert world.input_count(f"retired-{method}") == 0


def test_controller_bootstrap_input_obeys_retirement_fence(world):
    binding = world.prepare()
    snapshot = world.stopped(binding)
    assert operation(world.host, "retire")(binding, snapshot["revision"], reason="forced") == "retired"
    process = world.supervisor(binding, ready=False)
    # Early rejection before backend creation is also a valid retired-launch outcome.
    eventually(lambda: world.logs("terminal_ready") or process.poll() is not None,
               "retired bootstrap neither rejected nor reached terminal")
    assert world.logs("input_received") == [], "controller sent stage input after retirement"
    assert world.view(binding)["retired"] is True


def test_accepted_upstream_input_is_reserved_before_turn_started(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.stop_bootstrap(binding)
    world.configure(inputs={"held-input": "hold"})
    world.terminal(rpc("turn/start", "held-input"))
    eventually(lambda: world.input_count("held-input") == 1, "native input did not reach backend")
    current = world.view(binding)
    assert operation(world.host, "retire")(binding, current["revision"]) == "held"
    assert world.view(binding)["retired"] is False


def test_confirmed_rejection_releases_only_that_input(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.stop_bootstrap(binding)
    world.configure(inputs={"rejected-input": "reject"})
    world.terminal(rpc("turn/steer", "rejected-input", request_id="rejection"))
    reply = eventually(lambda: next((item["packet"] for item in world.logs("terminal_receive")
        if item["packet"].get("id") == "rejection"), None), "confirmed upstream rejection did not reach terminal")
    assert reply["error"]["code"] == -32602
    current = world.view(binding)
    assert operation(world.host, "retire")(binding, current["revision"]) == "retired"


def test_distinct_connections_reusing_rpc_id_keep_newer_input_reserved(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.stop_bootstrap(binding)
    world.configure(inputs={"older-input": "defer", "newer-input": "hold"})
    token = world.command("terminal", "connect", connection="secondary")
    eventually(lambda: any(item["token"] == token for item in world.logs("terminal_command")),
               "second terminal connection did not connect")
    world.terminal(rpc("turn/start", "older-input", request_id="reused"))
    world.terminal(rpc("turn/start", "newer-input", request_id="reused"), connection="secondary")
    eventually(lambda: world.input_count("older-input") == world.input_count("newer-input") == 1,
               "independent native inputs did not both reach backend")
    world.configure(inputs={"older-input": "reject", "newer-input": "hold"})
    rejected = eventually(lambda: next((item["packet"] for item in world.logs("terminal_receive")
        if item["connection"] == "primary" and item["packet"].get("id") == "reused"), None),
        "older input rejection did not reach its originating connection")
    assert rejected["error"]["code"] == -32602
    current = world.view(binding)
    assert operation(world.host, "retire")(binding, current["revision"]) == "held"


def test_lost_ack_and_repeated_client_message_do_not_resend_input(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.stop_bootstrap(binding)
    world.configure(inputs={"uncertain-input": "lose"})
    world.terminal(rpc("turn/start", "uncertain-input", request_id="lost-ack"))
    eventually(lambda: world.input_count("uncertain-input") == 1, "backend did not receive uncertain input")
    token = world.command("terminal", "connect", connection="repeated")
    eventually(lambda: any(item["token"] == token for item in world.logs("terminal_command")),
               "replacement terminal connection did not connect")
    world.terminal(rpc("turn/start", "uncertain-input", request_id="retry-ack"), connection="repeated")
    eventually(lambda: any(item["packet"].get("id") == "retry-ack" for item in world.logs("terminal_receive"))
               or world.input_count("uncertain-input") > 1, "repeated input received no rejection")
    assert world.input_count("uncertain-input") == 1, "uncertain input must not be blindly transmitted again"
    current = world.view(binding)
    assert operation(world.host, "retire")(binding, current["revision"]) != "retired"


def test_gateway_preserves_bidirectional_native_rpc_packets(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.terminal({"id": "terminal-read", "method": "thread/read", "params": {"threadId": ROOT}})
    reply = eventually(lambda: next((item["packet"] for item in world.logs("terminal_receive")
        if item["packet"].get("id") == "terminal-read"), None), "native thread/read response lost")
    assert reply["result"]["thread"]["id"] == ROOT
    for packet in ({"method": "item/started", "params": {"threadId": ROOT, "turnId": "turn-1",
                        "item": {"id": "native-item", "type": "reasoning"}}},
                   {"id": "server-request", "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": ROOT, "turnId": "turn-1", "itemId": "command-A"}}):
        world.command("backend", "broadcast", packet=packet)
        eventually(lambda: any(item["packet"] == packet for item in world.logs("terminal_receive")),
                   "native notification/server request lost on terminal path")
    response = {"id": "server-request", "result": {"decision": "decline"}}
    world.terminal(response)
    eventually(lambda: any(item["packet"] == response for item in world.logs("backend_receive")),
               "terminal response to native server request lost")


@pytest.mark.parametrize("method", ["thread/start", "thread/resume"])
def test_terminal_cannot_select_a_foreign_root(world, method):
    binding = world.prepare()
    world.supervisor(binding)
    before = world.view(binding)["binding"]
    packet = {"id": "foreign-selection", "method": method, "params": {"threadId": "foreign-root"}}
    world.terminal(packet)
    response = eventually(lambda: next((item["packet"] for item in world.logs("terminal_receive")
        if item["packet"].get("id") == "foreign-selection"), None), "foreign root request lacked rejection")
    assert "error" in response
    assert world.view(binding)["binding"] == before
    assert not any(item["packet"] == packet for item in world.logs("backend_receive"))


class Hooks:
    def __init__(self, world, binding):
        self.world, self.binding = world, binding
        agent = world.worktree / ".agent"
        agent.mkdir(exist_ok=True)
        (agent / "task.json").write_text(json.dumps({"target": TARGET, "issue": ISSUE}))
        workspace.install_stop_hook(str(world.worktree))
        self.settings = json.loads((world.worktree / ".claude/settings.local.json").read_text())
        binary_dir = world.root / "hook-bin"
        binary_dir.mkdir()
        binary = binary_dir / "curl"
        binary.write_text(f"#!{sys.executable}\n" + '''import os, subprocess, sys
arguments = sys.argv[1:]
for index, argument in enumerate(arguments):
    if argument == "--unix-socket":
        arguments[index + 1] = os.environ["T4_HOOK_SOCKET"]
sys.exit(subprocess.call(["/usr/bin/curl", *arguments]))
''')
        binary.chmod(0o755)
        self.env = world.env(binding)
        self.env.update(CLAUDE_PROJECT_DIR=str(world.worktree), T4_HOOK_SOCKET=str(world.state / "wait/wait.sock"),
                        PATH=f"{binary_dir}:{os.environ['PATH']}")

    def emit(self, kind, payload=None):
        if payload is None:
            payload = {"hook_event_name": kind, "session_id": ROOT, "prompt_id": "prompt-A",
                       "prompt": "fixture input", "permission_mode": "bypassPermissions",
                       "cwd": str(self.world.worktree)}
            if kind == "SessionStart":
                payload["source"] = "startup"
            if kind == "Stop":
                payload.update(background_tasks=[], stop_hook_active=False)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        results = []
        for entry in self.settings["hooks"][kind]:
            for hook in entry["hooks"]:
                results.append(subprocess.run(["/bin/bash", "-c", hook["command"]], input=text, text=True,
                    capture_output=True, cwd=self.world.worktree, env=self.env, timeout=6))
        assert results, f"native {kind} hook must be installed"
        return results


def test_installed_claude_hook_rejects_retired_main_input(world):
    binding = world.prepare(runtime="claude")
    hooks = Hooks(world, binding)
    assert all(item.returncode == 0 for item in hooks.emit("SessionStart"))
    assert all(item.returncode == 0 for item in hooks.emit("UserPromptSubmit"))
    assert all(item.returncode == 0 for item in hooks.emit("Stop"))
    snapshot = world.view(binding)
    assert operation(world.host, "retire")(binding, snapshot["revision"], reason="forced") == "retired"
    before = world.view(binding)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": ROOT, "prompt_id": "prompt-B", "prompt": "late"}
    assert all(item.returncode == 2 for item in hooks.emit("UserPromptSubmit", payload))
    assert world.view(binding) == before


@pytest.mark.parametrize("payload", ["{", "{}", '{"session_id":"root","prompt_id":null}',
    '{"hook_event_name":"Stop","session_id":"foreign","prompt_id":"prompt-A"}'])
def test_installed_claude_declared_input_fails_closed_on_bad_payload(world, payload):
    binding = world.prepare(runtime="claude")
    hooks = Hooks(world, binding)
    before = world.view(binding)
    assert all(item.returncode == 2 for item in hooks.emit("UserPromptSubmit", payload))
    assert world.view(binding) == before


def test_installed_claude_input_fails_closed_when_listener_receipt_is_uncertain(world):
    binding = world.prepare(runtime="claude")
    hooks = Hooks(world, binding)
    world.stop(world.listener)
    assert all(item.returncode == 2 for item in hooks.emit("UserPromptSubmit"))


def test_installed_claude_duplicate_prompt_does_not_admit_again(world):
    binding = world.prepare(runtime="claude")
    hooks = Hooks(world, binding)
    assert all(item.returncode == 0 for item in hooks.emit("SessionStart"))
    assert all(item.returncode == 0 for item in hooks.emit("UserPromptSubmit"))
    assert all(item.returncode == 0 for item in hooks.emit("Stop"))
    stopped = world.view(binding)
    assert all(item.returncode == 2 for item in hooks.emit("UserPromptSubmit"))
    assert world.view(binding) == stopped
    assert operation(world.host, "retire")(binding, stopped["revision"]) == "retired"


def test_installed_claude_supplemental_child_cannot_control_main_turn(world):
    binding = world.prepare(runtime="claude")
    hooks = Hooks(world, binding)
    before = world.view(binding)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": ROOT,
               "prompt_id": "child-prompt", "agent_id": "child-agent", "prompt": "supplemental"}
    assert all(item.returncode == 0 for item in hooks.emit("UserPromptSubmit", payload))
    assert world.view(binding) == before


def dispatcher_fixture(world, monkeypatch, binding, *, status="working", note="", alive=True):
    from dispatcher.config import Config, Target
    from dispatcher.main import Deps, run_pass
    from dispatcher.models import DEFAULT_POLICY
    from dispatcher.sessions import Sessions
    from dispatcher.state import Stage, TaskState, load, save
    from dispatcher import herdr, usage_providers
    from dispatcher.usage import ProviderUsage, Window, WindowKind
    from telegram import inbound

    external = []
    world.dispatcher_external = external
    class Tab:
        @property
        def alive(self):
            return alive
        @classmethod
        def find(cls, label):
            return cls()
        @classmethod
        def ensure(cls, workspace_label, label, cwd, env=None):
            return cls()
        def read(self, source, lines):
            return ""
        def agent_state(self):
            return None
        def run(self, command):
            external.append(("tab.run", command))
            return True
        def send_text(self, text):
            external.append(("tab.send_text", text))
            return True
        def send_keys(self, *keys):
            return True
        def close(self):
            external.append(("close", copy.deepcopy(world.view(binding))))
            return True

    class GitHub:
        def __getattr__(self, name):
            def invoke(*args, **kwargs):
                external.append((name, args, kwargs))
                if name in {"candidates", "rank_rows", "ci_statuses"}:
                    return []
                if name == "viewer_login":
                    return "fixture"
                if name == "issue_state":
                    return "OPEN"
                if name == "pr_number_for_branch":
                    return 17
                if name == "pr_view":
                    return {"state": "OPEN", "headRefOid": "fixture", "headRefName": "task/384",
                            "baseRefName": "main", "isDraft": False, "url": "https://invalid.test/pr/17"}
                if name == "issue_view":
                    return {"title": "fixture", "body": "", "state": "OPEN", "labels": []}
                return None
            return invoke

    class Notifier:
        def send(self, template, **ctx):
            external.append(("notify", template, ctx))
            return 1

    class Adapter:
        def __init__(self, name):
            self.name = name
        def fetch(self, state_dir, *, now=time.time):
            return ProviderUsage(provider=self.name, source="oauth", fetched_at=now(), windows=(
                Window(kind=WindowKind.SESSION, scope=None, used=0,
                       resets_at=datetime.now(timezone.utc) + timedelta(hours=5)),
                Window(kind=WindowKind.WEEKLY, scope=None, used=0,
                       resets_at=datetime.now(timezone.utc) + timedelta(days=7))))

    monkeypatch.setattr(herdr, "Tab", Tab)
    monkeypatch.setattr(inbound, "fetch_events", lambda state_dir: [])
    for provider in tuple(usage_providers.ADAPTERS):
        monkeypatch.setitem(usage_providers.ADAPTERS, provider, Adapter(provider))
    original_run = subprocess.run
    def external_run(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args[:3] == ["podman", "rm", "-f"]:
            external.append(("podman.rm", copy.deepcopy(world.view(binding))))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return original_run(args, *positional, **kwargs)
    monkeypatch.setattr(subprocess, "run", external_run)
    agent = world.worktree / ".agent"
    agent.mkdir(exist_ok=True)
    signal_path = agent / "stage.json"
    stage_signal = {"stage": "spec" if status == "awaiting-review" else "review", "status": status, "note": note}
    if status == "awaiting-answers":
        (agent / "answers.md").write_text("# Fixture answers\n")
        stage_signal["artifact"] = ".agent/answers.md"
    if status == "awaiting-ci":
        stage_signal["run_id"] = 123
    if status == "done":
        stage_signal["artifact"] = "https://github.com/fixture/repo/pull/123"
    signal_path.write_text(json.dumps(stage_signal))
    target = Target(name=TARGET, repo="fixture/repository", clone_path=str(world.root / "clone"),
        worktrees_path=str(world.root), rank_cmd="", project_number=1, project_owner="fixture",
        status_field_id="status", status_ready_option_id="ready", status_in_progress_option_id="running")
    config = Config(state_dir=str(world.state), capacity=1, session_memory="1g", session_cpus="1", targets=[target], spec_review_grace_minutes=0)
    task = TaskState(issue=ISSUE, target=TARGET, stage=Stage.AWAITING_SPEC_REVIEW if status == "awaiting-review" else Stage.REVIEW, slot=1, worktree=str(world.worktree),
        branch="task/384", title="T4 fixture", updated_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        track=DEFAULT_POLICY.untracked)
    save(world.state, task)
    dependencies = Deps(github=GitHub(), sessions=Sessions(state_dir=world.state), notifier=Notifier())
    external.append(("runtime_view_before", copy.deepcopy(dependencies.sessions.runtime_view(TARGET, ISSUE))))
    external.append(("is_alive_before", dependencies.sessions.is_alive(TARGET, ISSUE)))
    return lambda: run_pass(config, dependencies), lambda: load(world.state, TARGET, ISSUE), external


def test_dispatcher_admitted_input_holds_old_stop_without_ending_session(world, monkeypatch):
    binding = world.prepare(ticket="")
    world.stopped(binding)
    assert operation(world.host, "accept_input")(binding, "operator-in-flight") is True
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    run()
    task = load()
    assert task.park == ""
    assert not any(item[0] in {"close", "podman.rm"} for item in external)
    assert world.view(binding)["retired"] is False


def test_dispatcher_ordinary_input_park_fences_before_physical_end(world, monkeypatch):
    binding = world.prepare(ticket="")
    world.stopped(binding)
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    run()
    assert "session stopped mid-stage waiting for input" in load().park_note
    closures = [item[1] for item in external if item[0] in {"close", "podman.rm"}]
    assert closures, "ordinary stopped-input park must physically end session"
    assert all(view and view["retired"] is True for view in closures), "retirement must be durable before end"
    assert world.view(binding)["retired"] is True


def test_dispatcher_unknown_runtime_does_not_park_or_end(world, monkeypatch):
    binding = world.prepare(ticket="")
    world.stopped(binding)
    assert world.host.event(binding, {"type": "control/unknown", "message": "fixture lost visibility"})
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    run()
    assert load().park == ""
    assert not any(item[0] in {"close", "podman.rm"} for item in external)


@pytest.mark.parametrize("status", ["blocked", "awaiting-answers", "awaiting-ci", "awaiting-review"])
def test_explicit_stage_authority_fences_active_launch_before_end(world, monkeypatch, status):
    binding = world.prepare(ticket="", stage="spec" if status == "awaiting-review" else "review")
    assert world.host.event(binding, {"type": "service", "status": "live"})
    assert world.host.event(binding, {"type": "turn/started", "thread_id": ROOT, "turn_id": "active", "status": "inProgress"})
    run, load, external = dispatcher_fixture(world, monkeypatch, binding, status=status, note="fixture operator help")
    run()
    task = load()
    assert task.park or str(task.stage) not in {"review", "Stage.REVIEW"}, "explicit stage authority must still apply"
    closures = [item[1] for item in external if item[0] in {"close", "podman.rm"}]
    assert closures, "explicit stage transition must close physical session"
    assert all(view and view["retired"] is True for view in closures)
    assert operation(world.host, "accept_input")(binding, "after-stage-gate") is False


def test_dead_service_retains_failed_stage_and_fences_before_end(world, monkeypatch):
    from dispatcher.state import Stage
    binding = world.prepare(ticket="")
    assert world.host.event(binding, {"type": "service", "status": "dead"})
    run, load, external = dispatcher_fixture(world, monkeypatch, binding, alive=False)
    run()
    task = load()
    assert task.stage == Stage.FAILED
    assert task.crashed_stage == "review"
    closures = [item[1] for item in external if item[0] in {"close", "podman.rm"}]
    assert closures
    assert all(view and view["retired"] is True for view in closures)


def test_review_done_preserves_existing_pr_open_transition(world, monkeypatch):
    from dispatcher.state import Stage
    binding = world.prepare(ticket="")
    assert world.host.event(binding, {"type": "service", "status": "live"})
    assert world.host.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                      "turn_id": "active", "status": "inProgress"})
    run, load, external = dispatcher_fixture(world, monkeypatch, binding, status="done")
    run()
    assert load().stage == Stage.PR_OPEN


def test_stale_revision_holds_even_after_latest_turn_stops(direct):
    binding = prepare_direct(direct)
    old = stopped_direct(direct, binding)
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "turn-B", "status": "inProgress"})
    assert direct.event(binding, {"type": "turn/completed", "thread_id": ROOT,
                                  "turn_id": "turn-B", "status": "completed"})
    current = direct.view(TARGET, ISSUE)
    assert current["main"]["status"] == "stopped"
    assert current["revision"] != old["revision"]
    assert operation(direct, "retire")(binding, old["revision"]) != "retired"
    current = direct.view(TARGET, ISSUE)
    assert operation(direct, "retire")(binding, current["revision"]) == "retired"


def test_forced_retirement_preserves_explicit_authority_despite_active_input(direct):
    binding = prepare_direct(direct)
    assert direct.event(binding, {"type": "service", "status": "live"})
    assert direct.event(binding, {"type": "turn/started", "thread_id": ROOT,
                                  "turn_id": "turn-A", "status": "inProgress"})
    assert operation(direct, "accept_input")(binding, "foreground-input")
    active = direct.view(TARGET, ISSUE)
    assert operation(direct, "retire")(binding, active["revision"], reason="forced") == "retired"
    assert operation(direct, "accept_input")(binding, "after-closure") is False



def test_dispatcher_and_listener_input_race_cannot_end_admitted_work(world, monkeypatch):
    from threading import Barrier
    binding = world.prepare(ticket="")
    world.stopped(binding)
    admit = operation(world.host, "accept_input")
    run, load, external = dispatcher_fixture(world, monkeypatch, binding)
    barrier = Barrier(2)
    def input_request():
        barrier.wait()
        return admit(binding, "racing-main-input")
    def dispatcher_pass():
        barrier.wait()
        run()
    with ThreadPoolExecutor(max_workers=2) as executor:
        incoming = executor.submit(input_request)
        dispatch = executor.submit(dispatcher_pass)
        accepted = incoming.result()
        dispatch.result()
    task = load()
    closures = [item[1] for item in external if item[0] in {"close", "podman.rm"}]
    if accepted:
        assert task.park == ""
        assert closures == []
        assert world.view(binding)["retired"] is False
    else:
        assert task.park
        assert closures and all(view["retired"] is True for view in closures)



def test_active_terminal_steer_is_durable_and_preserves_exact_turn_precondition(world):
    binding = world.prepare()
    world.supervisor(binding)
    world.configure(hold_reads=True, inputs={"held-steer": "hold"})
    before = world.view(binding)
    packet = rpc("turn/steer", "held-steer", request_id="active-steer", turn="turn-1")
    world.terminal(packet)
    forwarded = eventually(lambda: next((item for item in world.logs("input_received")
        if item["client_id"] == "held-steer"), None), "active native steer did not reach backend")
    assert forwarded["packet"]["params"] == packet["params"]
    after = world.view(binding)
    assert after["revision"] > before["revision"], "input must be durably admitted before upstream forwarding"
    assert after["main"]["status"] == "active"



def test_concurrent_tasks_do_not_share_input_retirement_authority(world):
    from threading import Barrier
    first = world.prepare(ticket="", conversation=ROOT)
    second = world.prepare(issue=281, ticket="", conversation="other-root")
    first_stopped = world.stopped(first)
    for event in ({"type": "service", "status": "live"},
                  {"type": "turn/started", "thread_id": "other-root", "turn_id": "other-turn", "status": "inProgress"},
                  {"type": "inventory", "certainty": "known", "workers": []},
                  {"type": "turn/completed", "thread_id": "other-root", "turn_id": "other-turn", "status": "completed"}):
        assert world.host.event(second, event)
    second_stopped = world.view(second)
    admit = operation(world.host, "accept_input")
    retire = operation(world.host, "retire")
    barrier = Barrier(2)
    def first_input():
        barrier.wait()
        return admit(first, "first-task-input")
    def second_retirement():
        barrier.wait()
        return retire(second, second_stopped["revision"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        admitted = executor.submit(first_input)
        retired = executor.submit(second_retirement)
        assert admitted.result() is True
        assert retired.result() == "retired"
    assert world.view(first)["binding"] == first_stopped["binding"]
    assert world.view(first)["retired"] is False
    assert world.view(second)["retired"] is True
    current = world.view(first)
    assert retire(first, current["revision"]) == "held"
