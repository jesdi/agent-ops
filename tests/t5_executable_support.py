"""Owned executable product seam and independently authored native provider.

Only public RuntimeClient, process and native fake controls are used. Product
listener/controller/gateway code is neither imported as helpers nor mocked.
"""

import asyncio
from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from dispatcher.runtime_http import RuntimeClient
from t5_provider.ws_wire import control

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
PROVIDER_ROOT = Path(__file__).resolve().parent / "t5_provider"


def _wait_owned_group(process, *, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        process.poll()  # Reap the owning leader; descendants retain its group.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return process.poll() is not None
        except PermissionError:
            # Permission is uncertain existence, never disappearance. Darwin
            # can transiently deny this probe while a killed group exits.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _signal_owned_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin may report EPERM for a redundant KILL after the owned group
        # exits. Only an independent ESRCH observation authorizes that race.
        if signum != signal.SIGKILL or not _wait_owned_group(process):
            raise


def _stop_owned_group(process):
    _signal_owned_group(process, signal.SIGTERM)
    if _wait_owned_group(process):
        return True
    _signal_owned_group(process, signal.SIGKILL)
    return _wait_owned_group(process)


class ExecutableSession(AbstractContextManager):
    def __init__(self, *, script=None, resume=None, bootstrap_pending=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="t5x-", dir="/tmp")
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state"
        self.worktree = self.directory / "worktree"
        self.worktree.mkdir()
        self.state.mkdir()
        self.processes = []
        self.logs = []
        self.script = script
        self.resume = resume
        self.bootstrap_pending = bootstrap_pending
        self.client = RuntimeClient(self.state)
        self.backend_control = self.directory / "backend-control.sock"
        self.terminal_control = self.directory / "terminal-control.sock"
        self.env = {"PATH": str(Path(sys.executable).parent) + ":" + str(PROVIDER_ROOT) + ":/usr/bin:/bin",
                    "PYTHONPATH": str(PRODUCT_ROOT), "PYTHONPYCACHEPREFIX": str(self.directory / "bytecode"),
                    "HOME": str(self.directory / "home"), "CODEX_HOME": str(self.directory / "codex-home"),
                    "XDG_CONFIG_HOME": str(self.directory / "xdg-config"), "XDG_CACHE_HOME": str(self.directory / "xdg-cache"),
                    "XDG_DATA_HOME": str(self.directory / "xdg-data"), "HISTFILE": str(self.directory / "history"),
                    "AGENT_OPS_STATE_DIR": str(self.state), "FAKE_CODEX_CONTROL": str(self.backend_control),
                    "FAKE_CODEX_JOURNAL": str(self.directory / "native.jsonl"),
                    "FAKE_CODEX_CWD": str(self.worktree), "FAKE_TERMINAL_CONTROL": str(self.terminal_control),
                    "FAKE_TERMINAL_JOURNAL": str(self.directory / "terminal.jsonl")}
        for field in ["HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"]:
            Path(self.env[field]).mkdir()

    def spawn(self, args, name):
        stream = (self.directory / (name + ".log")).open("w+")
        self.logs.append(stream)
        process = subprocess.Popen(args, cwd=self.worktree, env=self.env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(process)
        return process

    def until(self, predicate, *, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.03)
        return False

    def view(self):
        return self.client.view("fixture", 501, self.binding["launch_id"])

    def backend(self, action, **fields):
        return asyncio.run(control(str(self.backend_control), {"action": action, **fields}))

    def terminal(self, action, **fields):
        return asyncio.run(control(str(self.terminal_control), {"action": action, **fields}))

    def rows(self, name="native.jsonl"):
        path = self.directory / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def start(self):
        self.listener = self.spawn([sys.executable, "-m", "dispatcher.waitd"], "listener")
        assert self.until(lambda: (self.state / "wait" / "wait.sock").exists(), seconds=5), self.diagnostics()
        prepared = self.client.prepare("fixture", 501, "review", runtime="codex",
                                       conversation_id=self.resume, worktree=str(self.worktree))
        self.binding = prepared["binding"]
        self.env.update({"AGENT_OPS_TARGET": "fixture", "AGENT_OPS_ISSUE": "501",
                         "AGENT_OPS_LAUNCH_ID": self.binding["launch_id"],
                         "AGENT_OPS_CONVERSATION_ID": self.resume or "", "AGENT_OPS_STAGE": "review",
                         "AGENT_OPS_TICKET": ""})
        prompt = self.directory / "prompt.txt"
        prompt.write_text("generic fixture stage input\n")
        if self.script is not None:
            script_path = self.directory / "native-script.json"
            script_path.write_text(json.dumps(self.script))
            self.env["FAKE_CODEX_SCRIPT"] = str(script_path)
        args = [sys.executable, "-P", "-m", "dispatcher.codex_supervisor", "--model", "fixture", "--prompt-file", str(prompt)]
        if self.resume:
            args.extend(["--resume", self.resume])
        self.supervisor = self.spawn(args, "supervisor")
        assert self.until(lambda: self.backend_control.exists(), seconds=5), self.diagnostics()
        if self.bootstrap_pending:
            assert self.resume is not None, "held-bootstrap fixture uses an explicit recorded root"
            self.root = self.resume
            self.initial_turn = None
            return self
        return self.finish_start()

    def finish_start(self):
        assert self.until(lambda: (self.view() or {}).get("main", {}).get("status") == "active", seconds=8), self.diagnostics()
        self.binding = self.view()["binding"]
        self.root = self.binding["conversation_id"]
        self.initial_turn = self.view()["main"]["turn_id"]
        assert self.until(self.terminal_control.exists, seconds=5), self.diagnostics()
        return self

    def diagnostics(self):
        content = []
        for stream in self.logs:
            stream.flush()
            stream.seek(0)
            content.append(stream.read()[-5000:])
        return "\n".join(content)

    def __enter__(self):
        try:
            return self.start()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def restart_listener(self):
        self.listener.terminate()
        self.listener.wait(timeout=5)
        self.listener = self.spawn([sys.executable, "-m", "dispatcher.waitd"], "listener-restarted")
        def ready():
            try:
                return self.client.event(self.binding, {"type": "service", "status": "live"})
            except (OSError, ValueError):
                return False
        assert self.until(ready, seconds=5), self.diagnostics()

    def __exit__(self, exc_type, exc, traceback):
        failures = []
        for path, action in [(self.terminal_control, self.terminal), (self.backend_control, self.backend)]:
            if path.exists():
                try:
                    action("shutdown")
                except (OSError, ValueError):
                    pass
        for process in reversed(self.processes):
            # Each spawned process owns a distinct group; a reaped leader does
            # not establish that its descendants stopped writing.
            if not _stop_owned_group(process):
                failures.append(process.pid)
        assert not failures, f"owned fixture process groups failed cleanup: {failures}"
        for stream in self.logs:
            stream.close()
        self.temporary.cleanup()


def worker(snapshot, item_id, owner=None):
    return next((value for value in snapshot["workers"] if value["identity"]["kind"] == "command"
                 and value["identity"]["initial_item_id"] == item_id
                 and (owner is None or value["identity"]["thread_id"] == owner)), None)


def result(snapshot, item_id):
    return next((value for value in snapshot["completions"] if value["identity"]["kind"] == "command"
                 and value["identity"]["initial_item_id"] == item_id), None)
