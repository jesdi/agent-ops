"""Locked T3 contract: Sessions/container command and executable supervisor.

Only external herdr, Podman launch capture and Codex processes are faked.
The host listener, binding policy, state and supervisor remain production code.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import pytest
import websockets

from dispatcher import containers, herdr, waitd
from dispatcher.runtime_http import RuntimeClient
from dispatcher.sessions import Sessions


REPO = Path(__file__).resolve().parents[1]
PROVIDER = Path(__file__).with_name("t3_fake_codex.py")
PROMPT = 'Review the literal $(touch should-not-exist) and `uname`\nThen check café paths.'


def eventually(predicate, message, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    assert predicate(), message


def terminate(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    # The supervisor may have exited while its fake terminal/backend survived.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        # Darwin can briefly deny a signal to an already-reaped empty group.
        # Confirm absence; never suppress permission failures for a live group.
        deadline = time.monotonic() + .2
        while time.monotonic() < deadline:
            time.sleep(.01)
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                continue
        raise error


@pytest.fixture
def isolated(monkeypatch):
    # Short paths are necessary for macOS's Unix socket path limit.
    with tempfile.TemporaryDirectory(prefix="t3-", dir="/tmp") as directory:
        root = Path(directory)
        state = root / "state"
        home = root / "home"
        worktree = root / "task"
        for path in (state, home, home / ".codex"):
            path.mkdir(parents=True, exist_ok=True)
        clone = root / "clone"
        subprocess.run(["git", "init", "-q", str(clone)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "-c", "user.name=T3 fixture",
                        "-c", "user.email=t3@example.invalid", "commit", "-q", "--allow-empty",
                        "-m", "Isolated task fixture"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b", "task/review",
                        str(worktree)], check=True, capture_output=True)
        (worktree / ".agent").mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
        monkeypatch.setenv("AGENT_OPS_STATE_DIR", str(state))
        monkeypatch.delenv("AGENT_OPS_COMMAND_WRAPPER", raising=False)
        output = (root / "listener.log").open("w+")
        listener = subprocess.Popen(
            [sys.executable, "-P", "-c", "import os; from dispatcher.waitd import serve, sock_path; "
             "s=os.environ['AGENT_OPS_STATE_DIR']; serve(sock_path(s),s)"],
            cwd=worktree, env={**os.environ, "PYTHONPATH": str(REPO)},
            stdout=output, stderr=output, start_new_session=True,
        )
        try:
            eventually(lambda: waitd.sock_path(state).exists(), "isolated runtime listener must start",
                       timeout=2)
            yield root, state, worktree
        finally:
            terminate(listener)
            output.close()


class CapturedTab:
    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return True

    def alive(self):
        return True

    def close(self):
        return True

    def agent_state(self):
        return "working", 1


@pytest.fixture
def launches(monkeypatch):
    tabs = {}

    def ensure(workspace_label, label, cwd, env=None):
        tab = tabs.setdefault(label, CapturedTab())
        return tab

    monkeypatch.setattr(herdr.Tab, "ensure", ensure)
    monkeypatch.setattr(herdr.Tab, "find", lambda label: tabs.get(label))
    return tabs


def podman_parts(command):
    words = shlex.split(command)
    mounts, environment = [], {}
    for index, word in enumerate(words[:-1]):
        if word in {"-v", "--volume"}:
            mounts.append(words[index + 1])
        elif word in {"-e", "--env"}:
            key, separator, value = words[index + 1].partition("=")
            if separator:
                environment[key] = value
    return words, mounts, environment


def shell_words(command):
    words = shlex.split(command)
    for index, word in enumerate(words[:-2]):
        if Path(word).name in {"sh", "bash"} and words[index + 1] in {"-c", "-lc"}:
            return shlex.split(words[index + 2])
    return words


def supervisor_arguments(command):
    words = shell_words(command)
    assert "dispatcher.codex_supervisor" in words, (
        "Codex task sessions must execute the controlled supervisor inside their container")
    position = words.index("dispatcher.codex_supervisor")
    assert words[position - 1] == "-m" and "-P" in words[:position], (
        "the deployed Python package must run in safe-path mode")
    return words[position + 1:]


def launch_stage(state, worktree, launches, *, target="project-a", issue=370, effort="high"):
    session = Sessions(memory="7g", cpus="3.5", state_dir=state)
    session.spawn_stage(target, issue, str(worktree), PROMPT, "review", "openai/gpt-6.1-sol", effort)
    return session, launches[f"task-{target}-{issue}"].commands[-1]


def test_container_mounts_readonly_supervisor_and_websocket_dependency_without_host_state(isolated):
    _, state, worktree = isolated
    state = state.resolve()
    binding = RuntimeClient(state).prepare("project-a", 370, "review", worktree=str(worktree))["binding"]
    command = containers.session_cmd("task-project-a-370", str(worktree), "7g", "3.5",
        "openai/gpt-6.1-sol", "--prompt-file .agent/prompt-review.md", effort="high",
        launch_env={f"AGENT_OPS_{key.upper()}": str(value or "") for key, value in binding.items()})
    words, mounts, environment = podman_parts(command)
    source_mounts = []
    dependency_mounts = []
    for mount in mounts:
        source, destination, *options = mount.split(":")
        host_path = Path(source)
        if (host_path / "dispatcher" / "codex_supervisor.py").is_file():
            source_mounts.append((source, destination, options))
        if (host_path / "websockets" / "__init__.py").is_file():
            dependency_mounts.append((source, destination, options))
        assert source != str(state), "the full state directory must stay on the host"
        assert "runtime-snapshots" not in source and "runtime-host" not in source, (
            "runtime snapshots and the host credential must not enter task containers")
        if host_path.is_relative_to(state):
            assert host_path in {state / "wait", state / "codex-home"}, (
                "only the wait socket directory and runtime home may be mounted from state")
        if host_path.is_dir() and "ro" not in options:
            assert host_path != state and host_path not in state.parents
    assert source_mounts and all("ro" in options for _, _, options in source_mounts), (
        "the supervisor's deployed Python package must be mounted read-only")
    assert dependency_mounts and all("ro" in options for _, _, options in dependency_mounts), (
        "the installed Python WebSocket dependency must be mounted read-only")
    for _, destination, _ in source_mounts + dependency_mounts:
        assert destination in environment.get("PYTHONPATH", "").split(":"), (
            "safe-path Python must find both deployed packages through explicit mounted paths")
    assert str(waitd.sock_path(state).parent) in [mount.split(":")[0] for mount in mounts]


def test_sessions_preserve_stage_model_effort_resources_and_literal_prompt_file(isolated, launches):
    _, state, worktree = isolated
    _, command = launch_stage(state, worktree, launches)
    words, _, _ = podman_parts(command)
    assert words[words.index("--memory") + 1] == "7g"
    assert words[words.index("--cpus") + 1] == "3.5"
    arguments = supervisor_arguments(command)
    assert arguments[arguments.index("--model") + 1] == "gpt-6.1-sol"
    assert arguments[arguments.index("--effort") + 1] == "high"
    prompt_file = arguments[arguments.index("--prompt-file") + 1]
    assert (worktree / prompt_file).read_text() == PROMPT
    assert PROMPT not in command and "$(cat" not in command, (
        "the real stage prompt must be read by the supervisor without shell expansion")
    with supervisor(isolated) as running:
        running.ready()
        configuration = [record["params"] for record in running.records("rpc")
                         if record["method"] in {"thread/start", "thread/resume", "turn/start"}]
        backend_args = running.records("backend-started")[0]["argv"]
        overrides = [value.split("=", 1) for index, value in enumerate(backend_args)
                     if index and backend_args[index - 1] == "-c" and "=" in value]
        overrides = {key: value.strip('\"\'') for key, value in overrides}
        assert any(params.get("model") == "gpt-6.1-sol" for params in configuration) or (
            overrides.get("model") == "gpt-6.1-sol"), "the backend must receive the selected stage model"
        assert any(params.get("effort") == "high" or
                   (params.get("config") or {}).get("model_reasoning_effort") == "high"
                   for params in configuration) or overrides.get("model_reasoning_effort") == "high", (
            "the actual prompt must retain the selected reasoning effort")


@pytest.mark.parametrize("conversation", ["recorded-main", "--last", "--effort=high"])
def test_sessions_fresh_and_explicit_resume_have_fresh_launch_fences_without_last_selection(
        isolated, launches, conversation):
    _, state, worktree = isolated
    session, fresh_command = launch_stage(state, worktree, launches)
    fresh = session.runtime_view("project-a", 370)["binding"]
    assert fresh["conversation_id"] is None
    assert RuntimeClient(state).event(fresh, {"type": "bound", "conversation_id": conversation})
    session.resume("project-a", 370, str(worktree), PROMPT, "openai/gpt-6.1-sol",
                   effort="high", session_id=conversation)
    resumed = session.runtime_view("project-a", 370)["binding"]
    assert resumed["launch_id"] != fresh["launch_id"]
    assert resumed["conversation_id"] == conversation
    resumed_args = supervisor_arguments(launches["task-project-a-370"].commands[-1])
    assert f"--resume={conversation}" in resumed_args or (
        "--resume" in resumed_args and resumed_args[resumed_args.index("--resume") + 1] == conversation)
    session.spawn_stage("project-b", 281, str(worktree), PROMPT, "review", "openai/gpt-6.1-sol")
    second = session.runtime_view("project-b", 281)["binding"]
    assert second["launch_id"] not in {fresh["launch_id"], resumed["launch_id"]}
    assert second["conversation_id"] is None
    assert "--resume" not in supervisor_arguments(fresh_command)
    assert "--resume" not in supervisor_arguments(launches["task-project-b-281"].commands[-1])


class RunningSupervisor:
    def __init__(self, root, state, worktree, binding, process, output):
        self.root, self.state, self.worktree = root, state, worktree
        self.binding, self.process, self.output = binding, process, output

    def records(self, kind=None):
        path = self.root / "provider.jsonl"
        if not path.exists():
            return []
        # Concurrent append may leave the last line in progress for this read.
        rows = []
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or row["kind"] == kind:
                rows.append(row)
        return rows

    def snapshot(self):
        return RuntimeClient(self.state).view(self.binding["target"], self.binding["issue"],
                                              self.binding["launch_id"])

    def command(self, action, **fields):
        with (self.root / "commands.jsonl").open("a") as stream:
            stream.write(json.dumps(dict(action=action, **fields)) + "\n")

    def diagnostics(self):
        self.output.flush()
        return (self.root / "supervisor.log").read_text()

    def ready(self):
        eventually(lambda: self.records("terminal-attached"),
                   "the executable must run an app-server and attach a real remote terminal; " + self.diagnostics())
        return self.snapshot()["binding"]["conversation_id"]


@contextmanager
def supervisor(isolated, *, resume=None, mode="compatible"):
    root, state, worktree = isolated
    fixture = root / "provider"
    fixture.mkdir()
    (fixture / "mode").write_text(mode)
    binary_dir = root / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text(f"#!{sys.executable}\nexec(compile(open({str(PROVIDER)!r}).read(), {str(PROVIDER)!r}, 'exec'))\n")
    binary.chmod(0o755)
    prompt = worktree / ".agent" / "prompt-review.md"
    prompt.write_text(PROMPT)
    binding = RuntimeClient(state).prepare("project-a", 370, "review", ticket="4", runtime="codex",
                                conversation_id=resume, worktree=str(worktree))["binding"]
    environment = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(REPO) + os.pathsep + str(Path(websockets.__file__).resolve().parent.parent),
        "T3_PROVIDER_FIXTURE": str(fixture),
        **{f"AGENT_OPS_{key.upper()}": str(value or "") for key, value in binding.items()}}
    # A target package named dispatcher must not shadow the deployed supervisor.
    shadow = worktree / "dispatcher"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('unsafe target package imported')\n")
    args = [sys.executable, "-P", "-m", "dispatcher.codex_supervisor", "--model", "gpt-6.1-sol",
            "--prompt-file", str(prompt), "--effort", "high"]
    if resume is not None:
        args.append(f"--resume={resume}")
    output = (fixture / "supervisor.log").open("w+")
    process = subprocess.Popen(args, cwd=worktree, env=environment, stdout=output,
                               stderr=output, start_new_session=True)
    running = RunningSupervisor(fixture, state, worktree, binding, process, output)
    try:
        yield running
    finally:
        terminate(process)
        output.close()


def test_backend_control_and_attached_terminal_observe_one_bound_conversation_and_output(isolated):
    with supervisor(isolated) as running:
        identity = running.ready()
        assert identity
        assert running.snapshot()["service"] == "live"
        attached = running.records("terminal-attached")
        assert {record["thread_id"] for record in attached} == {identity}
        assert running.records("terminal-started")[0]["binding"] == running.snapshot()["binding"], (
            "binding must precede attachment to the real terminal")
        rpc = running.records("rpc")
        roots = {record["params"]["threadId"] for record in rpc if "threadId" in record["params"]}
        assert roots == {identity}, "controller and terminal must select only the prepared root"
        assert len(running.records("backend-started")) == 1
        assert len(running.records("terminal-started")) == 1
        running.command("output", threadId=identity, text="Same-terminal continuation: exit status 7")
        eventually(lambda: any("Same-terminal continuation: exit status 7" in json.dumps(record)
                               for record in running.records("terminal-output")),
                   "continuation must remain visible through the original attached terminal")
        assert running.records("terminal-output")[0]["pid"] == attached[0]["pid"]


def test_actual_initial_prompt_occurs_once_after_binding_without_a_synthetic_turn(isolated):
    with supervisor(isolated) as running:
        identity = running.ready()
        eventually(lambda: running.snapshot()["main"]["status"] == "active",
                   "authoritative actual main turn start must reach the listener")
        starts = [record for record in running.records("rpc") if record["method"] == "turn/start"]
        assert len(starts) == 1, "binding must not create a synthetic initial model turn"
        assert starts[0]["params"]["threadId"] == identity
        assert len(starts[0]["params"]["input"]) == 1
        assert starts[0]["params"]["input"][0]["type"] == "text"
        assert starts[0]["params"]["input"][0]["text"] == PROMPT
        assert running.records("prompt-binding")[0]["binding"] == running.snapshot()["binding"], (
            "the listener must bind the root before the actual prompt reaches the provider")
        assert not (running.worktree / "should-not-exist").exists()


@pytest.mark.parametrize("conversation", [None, "recorded-main", "--last", "--effort=high"])
def test_executable_selects_fresh_or_exact_literal_resume_without_other_roots(isolated, conversation):
    with supervisor(isolated, resume=conversation) as running:
        identity = running.ready()
        rpc = running.records("rpc")
        creates = [record for record in rpc if record["method"] == "thread/start"]
        resumes = [record for record in rpc if record["method"] == "thread/resume"]
        if conversation is None:
            assert len(creates) == 1 and identity == "fixture-main-1"
        else:
            assert not creates, "an explicit resume must not create a replacement conversation"
            assert identity == conversation
            assert resumes and {record["params"]["threadId"] for record in resumes} == {conversation}
        assert {record["params"]["threadId"] for record in rpc if "threadId" in record["params"]} == {identity}


def test_backend_death_ends_session_even_while_remote_terminal_process_survives(isolated):
    with supervisor(isolated, mode="terminal-survives") as running:
        running.ready()
        backend = running.records("backend-started")[0]["pid"]
        terminal = running.records("terminal-started")[0]["pid"]
        os.kill(backend, signal.SIGTERM)
        eventually(lambda: running.process.poll() is not None,
                   "backend death must end the session instead of leaving a live terminal shell")
        eventually(lambda: running.snapshot()["service"] == "dead",
                   "backend death must be visible as a launch-bound service crash")
        assert running.process.returncode != 0
        assert terminal != backend


def test_control_disconnect_reconnect_preserves_backend_terminal_launch_and_exact_root(isolated):
    with supervisor(isolated) as running:
        identity = running.ready()
        eventually(lambda: running.snapshot()["main"]["status"] == "active",
                   "the actual bound main turn must be visible before its controller disconnects")
        eventually(lambda: any(record["method"] == "thread/backgroundTerminals/list"
                               for record in running.records("rpc")), "controller must read owned-work inventory")
        before = running.snapshot()
        backend = running.records("backend-started")[0]["pid"]
        terminal = running.records("terminal-started")[0]["pid"]
        last_connection = max(record["connection"] for record in running.records("connected"))
        running.command("disconnect-control")
        dropped = eventually(lambda: running.records("control-disconnected"),
                             "fixture must disconnect only the control connection")[-1]["connections"]
        assert dropped, "test must actually sever a live control WebSocket"
        eventually(lambda: any(record["connection"] > last_connection and
                               record["method"] in {"thread/read", "thread/resume", "thread/backgroundTerminals/list"}
                               and record["params"].get("threadId") == identity
                               for record in running.records("rpc")),
                   "control must reconnect and recover the exact bound conversation")
        def recovered_controller():
            records = running.records("rpc")
            subscribed = {record["connection"] for record in records
                          if record["connection"] > last_connection and
                          record["method"] == "thread/resume" and
                          record["params"].get("threadId") == identity}
            return any(record["connection"] in subscribed and
                       record["method"] == "thread/backgroundTerminals/list" and
                       record["params"].get("threadId") == identity for record in records)

        eventually(recovered_controller,
                   "a new controller connection must reacquire both the root subscription and inventory")
        after = running.snapshot()
        assert after["binding"] == before["binding"]
        assert after["wait"] == before["wait"]
        assert after["service"] == "live" and running.process.poll() is None
        assert [record["pid"] for record in running.records("backend-started")] == [backend]
        assert [record["pid"] for record in running.records("terminal-started")] == [terminal]
        assert len([record for record in running.records("rpc") if record["method"] == "thread/start"]) == 1
        running.command("output", threadId=identity, text="Visible after control recovery")
        eventually(lambda: any("Visible after control recovery" in json.dumps(record)
                               for record in running.records("terminal-output")),
                   "the same attached terminal must display output after control reconnect")
        running.command("stop", threadId=identity)
        eventually(lambda: running.snapshot()["main"]["status"] == "stopped" and
                               running.snapshot()["main"]["turn_id"] == before["main"]["turn_id"],
                   "the matching normal completion after reconnect must reach the real host listener")
        stopped = running.snapshot()
        assert stopped["binding"] == before["binding"]
        assert stopped["wait"] == before["wait"]
        assert stopped["service"] == "live" and running.process.poll() is None
        assert [record["pid"] for record in running.records("backend-started")] == [backend]
        assert [record["pid"] for record in running.records("terminal-started")] == [terminal]
        assert len([record for record in running.records("rpc") if record["method"] == "turn/start"]) == 1


@pytest.mark.parametrize("mode", ["unsupported", "malformed"])
def test_incompatible_required_inventory_stays_unknown_and_reports_launch_bound_problem(isolated, mode):
    with supervisor(isolated, mode=mode) as running:
        eventually(lambda: running.snapshot()["alerts"],
                   "unsupported required interface/payload must report a task-bound compatibility problem")
        snapshot = running.snapshot()
        assert snapshot["binding"]["launch_id"] == running.binding["launch_id"]
        assert snapshot["inventory"] == "unknown"
        assert snapshot["main"]["status"] != "stopped"
        assert snapshot["workers"] == [] and snapshot["completions"] == []
        assert snapshot["binding"]["conversation_id"], "the problem must identify the selected root"
        assert any(word in json.dumps(snapshot["alerts"]).lower()
                   for word in ("compatib", "inventory", "unsupported", "payload"))
        assert running.process.poll() is None, "unknown control data alone must not crash the backend"
        assert not any(record["method"] in {"turn/interrupt", "legacy/notify"}
                       for record in running.records("rpc"))
