"""OWN public-boundary fixture. Nothing in this module executes on import.

Host execution was activated against the source bound in README.md.
No existing test fixture or product-private helper is imported.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from dataclasses import asdict
from datetime import datetime, timezone
from datetime import timedelta

CLEANUP_AUDITS = []


def isolated_environment(root, state_dir):
    """Allowlisted OWN process environment; no inherited account/config secrets."""
    root = Path(root)
    environment = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8",
                   "PYTHONPATH": os.pathsep.join([str(Path.cwd()), str(Path(__file__).parent)]),
                   "PYTHONDONTWRITEBYTECODE": "1",
                   "AGENT_OPS_STATE_DIR": str(state_dir)}
    for variable, directory in {
        "HOME": "home", "XDG_CONFIG_HOME": "xdg/config", "XDG_STATE_HOME": "xdg/state",
        "XDG_DATA_HOME": "xdg/data", "XDG_CACHE_HOME": "xdg/cache", "XDG_RUNTIME_DIR": "xdg/run",
        "CODEX_HOME": "codex-home", "CLAUDE_CONFIG_DIR": "claude-home", "TMPDIR": "tmp",
    }.items():
        path = root / directory
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        environment[variable] = str(path)
    owned_session = "t8-" + root.name
    environment["HERDR_SESSION"] = owned_session
    environment["HERDR_CONFIG_PATH"] = str(root / "herdr.toml")
    sockets = Path(environment["XDG_CONFIG_HOME"]) / "herdr" / "sessions" / owned_session
    sockets.mkdir(parents=True, exist_ok=True)
    environment["HERDR_SOCKET_PATH"] = str(sockets / "herdr.sock")
    environment["HERDR_CLIENT_SOCKET_PATH"] = str(sockets / "herdr-client.sock")
    # This optional installed executable selector contains no service credentials.
    if os.environ.get("AGENT_OPS_HERDR"):
        environment["AGENT_OPS_HERDR"] = os.environ["AGENT_OPS_HERDR"]
    return environment


def public_module(name):
    return importlib.import_module(name)


def presentation_capability():
    try:
        module = public_module("dispatcher.runtime_presentation")
    except ModuleNotFoundError as error:
        if error.name == "dispatcher.runtime_presentation":
            return None
        raise
    candidate = getattr(module, "present_runtime_alerts", None)
    return candidate if callable(candidate) else None


def decode_http(connection):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = connection.recv(65536)
        if not part:
            raise EOFError("owned HTTP peer closed before headers")
        data.extend(part)
    head, body = bytes(data).split(b"\r\n\r\n", 1)
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.strip().lower()] = value.strip()
    if b"content-length" in headers:
        remaining = int(headers[b"content-length"]) - len(body)
        while remaining > 0:
            part = connection.recv(min(remaining, 65536))
            if not part:
                raise EOFError("owned HTTP peer closed before body")
            body += part
            remaining -= len(part)
    else:
        # Valid ordinary HTTP response bodies may be delimited by connection close.
        while True:
            part = connection.recv(65536)
            if not part:
                break
            body += part
    return head + b"\r\n\r\n" + body, head.split(b"\r\n")[0], headers, body


def wire_request(path, route, payload, host_token=None):
    body = json.dumps(payload).encode()
    lines = [f"POST {route} HTTP/1.1", "Host: fixture",
             "Content-Type: application/json", f"Content-Length: {len(body)}",
             "Connection: close"]
    if host_token is not None:
        lines.append(f"X-Runtime-Host: {host_token}")
    request = "\r\n".join(lines).encode() + b"\r\n\r\n" + body
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(request)
        _, first, _, response = decode_http(connection)
    return int(first.split()[1]), json.loads(response)


class WireProxy:
    """Transparent OWN HTTP transport with an after-real-claim barrier/drop.

    Every normal byte is forwarded unchanged. Faults may omit a reply, never forge
    an affirmative claim or substitute a runtime view. Hooks are fixture transport
    actions, not product callbacks or presentation logic.
    """
    def __init__(self, public_path, backend_path):
        self.path = Path(public_path)
        self.backend_path = Path(backend_path)
        self.server = None
        self.thread = None
        self.stop = threading.Event()
        self.after_new_claim = None
        self.drop_new_claim_reply = False
        self.claim_committed = threading.Event()
        self.release_reply = None
        self.records = []
        self.errors = []
        self.handlers = []
        self.guard = threading.Lock()

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(32)
        self.server.settimeout(.1)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop.is_set():
            try:
                incoming, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            handler = threading.Thread(target=self._relay, args=(incoming,), daemon=True)
            self.handlers.append(handler)
            handler.start()

    def _relay(self, incoming):
        try:
            with incoming:
                incoming.settimeout(10)
                raw, first, headers, body = decode_http(incoming)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as backend:
                    backend.settimeout(10)
                    backend.connect(str(self.backend_path))
                    backend.sendall(raw)
                    reply, response_first, _, response_body = decode_http(backend)
                payload = json.loads(body)
                status = int(response_first.split()[1])
                value = json.loads(response_body)
                record = {"route": first.split()[1].decode(), "body": payload,
                          "host_authenticated": b"x-runtime-host" in headers,
                          "status": status, "value": value}
                with self.guard:
                    self.records.append(record)
                event = payload.get("event", {})
                if event.get("type") == "alert/presentation-claimed" and value is True:
                    self.claim_committed.set()
                    action = self.after_new_claim
                    if action is not None:
                        self.after_new_claim = None
                        action(record)
                    if self.release_reply is not None:
                        if not self.release_reply.wait(10):
                            raise TimeoutError("OWN claim response barrier not released")
                    if self.drop_new_claim_reply:
                        self.drop_new_claim_reply = False
                        return
                incoming.sendall(reply)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Deliberate owned client death may close this external connection.
        except BaseException as error:
            self.errors.append(error)

    def close(self):
        self.stop.set()
        if self.release_reply is not None:
            self.release_reply.set()
        if self.server is not None:
            self.server.close()
        if self.thread is not None:
            self.thread.join(2)
        for handler in self.handlers:
            handler.join(2)
        self.path.unlink(missing_ok=True)


class OwnedListener:
    def __init__(self, state_dir, proxy=False, environment=None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "wait" / "wait.sock"
        self.backend = self.path.with_name("owned-backend.sock") if proxy else self.path
        self.proxy = WireProxy(self.path, self.backend) if proxy else None
        self.process = None
        self.log = None
        self.environment = environment

    def start(self):
        if self.proxy is not None and self.proxy.stop.is_set():
            self.proxy = WireProxy(self.path, self.backend)
        self.backend.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(self.state_dir / "owned-listener.log", "ab")
        script = ("import sys; from public_errors import install; install(); from dispatcher.waitd import serve; "
                  "serve(sys.argv[1], sys.argv[2])")
        self.process = subprocess.Popen([sys.executable, "-c", script,
                                         str(self.backend), str(self.state_dir)],
                                        stdout=self.log, stderr=self.log, env=self.environment)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                message = (self.state_dir / "owned-listener.log").read_text()
                self.close()
                raise RuntimeError("real OWN listener exited: " + message)
            try:
                token = (self.state_dir / "runtime-host-token").read_text().strip()
                status, view = wire_request(self.backend, "/runtime/view",
                                            {"target": "owned-readiness", "issue": 1}, token)
                if status == 200 and view is None:
                    if self.proxy is not None:
                        self.proxy.start()
                    return
            except (OSError, EOFError, ValueError):
                pass
            time.sleep(.01)
        self.close()
        raise TimeoutError("real OWN listener did not answer a valid HTTP view")

    def close(self):
        if self.proxy is not None:
            self.proxy.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()

    def restart(self):
        old_inode = self.backend.stat().st_ino
        self.close()
        if self.proxy is not None:
            self.proxy = WireProxy(self.path, self.backend)
        self.start()
        return old_inode, self.backend.stat().st_ino


class RecordingNotifier:
    def __init__(self, return_id=71, error=None, before_return=None):
        self.calls = []
        self.return_id = return_id
        self.error = error
        self.before_return = before_return

    def send(self, template, **ctx):
        self.calls.append({"template": template, "context": copy.deepcopy(ctx)})
        if self.before_return is not None:
            action, self.before_return = self.before_return, None
            action()
        if self.error is not None:
            raise self.error
        return self.return_id


def values(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from values(child)
    elif isinstance(value, list):
        for child in value:
            yield from values(child)


def identity_in_detail(detail, identity):
    return any(isinstance(node, dict) and all(node.get(k) == v for k, v in identity.items())
               for node in values(detail))


class Host:
    def __init__(self, case, *, proxy=False):
        self.case = case
        self.temporary = tempfile.TemporaryDirectory(prefix="t8-h-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.children = []
        self.listener = None
        self.case.addCleanup(self.close)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.environment = isolated_environment(self.root, self.state_dir)
        self.listener = OwnedListener(self.state_dir, proxy=proxy, environment=self.environment)
        self.state = public_module("dispatcher.state")
        self.config = public_module("dispatcher.config")
        self.eventlog = public_module("dispatcher.eventlog")
        self.messages = public_module("dispatcher.messages")
        self.http = public_module("dispatcher.runtime_http")
        self.client = self.http.RuntimeClient(self.state_dir)
        self.targets = {}
        self.tasks = {}
        self.bindings = {}
        self.bootstrap_operations = {}
        self.bootstrap_inputs = {}
        self.turn_sequence = {}
        self.generated_task_paths = {}
        self.listener.start()

    def close(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        if self.listener is not None:
            self.listener.close()
        self.temporary.cleanup()
        proxy = self.listener.proxy if self.listener is not None else None
        audit = {"case": self.case.id(), "root": str(self.root),
                 "root_removed": not self.root.exists(),
                 "listener_stopped": self.listener is None or self.listener.process is None or self.listener.process.poll() is not None,
                 "children_stopped": all(child.poll() is not None for child in self.children),
                 "proxy_threads_stopped": proxy is None or
                 ((proxy.thread is None or not proxy.thread.is_alive()) and
                  all(not handler.is_alive() for handler in proxy.handlers))}
        CLEANUP_AUDITS.append(audit)
        self.case.assertTrue(all(audit[field] for field in
                             ("root_removed", "listener_stopped", "children_stopped", "proxy_threads_stopped")),
                             "OWN host resources must be fully released: " + repr(audit))

    def task(self, name="fixture", issue=1, stage="IMPLEMENT", ticket=1,
             *, unbound=False):
        key = (name, issue)
        worktree = self.root / f"worktree-{name}-{issue}"
        worktree.mkdir(exist_ok=True)
        clone = self.root / f"clone-{name}"
        clone.mkdir(exist_ok=True)
        target = self.config.Target(name=name, repo="fixture-owner/fixture-repo",
                                   clone_path=str(clone), worktrees_path=str(self.root),
                                   rank_cmd="", project_number=1, project_owner="",
                                   status_field_id="", status_ready_option_id="",
                                   status_in_progress_option_id="", gate_cmd="true")
        saved_stage = getattr(self.state.Stage, stage)
        continued = "spec" if stage == "AWAITING_SPEC_REVIEW" else saved_stage.value
        task = self.state.TaskState(issue=issue, target=name, stage=saved_stage, slot=1,
                                    worktree=str(worktree), branch=f"fixture-{name}-{issue}",
                                    title=f"OWN task {name} {issue}",
                                    updated_at=datetime.now(timezone.utc).isoformat(),
                                    ticket_cursor=ticket if stage == "IMPLEMENT" else 0,
                                    ticket_count=3)
        before = set(self.state_dir.rglob("*"))
        self.state.save(self.state_dir, task)
        created = [p for p in set(self.state_dir.rglob("*")) - before if p.is_file()]
        self.case.assertTrue(created or key in self.generated_task_paths,
                             "public save must create OWN task artifact")
        if created:
            self.generated_task_paths[key] = created
        self.targets[key], self.tasks[key] = target, task
        self.signal(key, "working")
        snapshot = self.client.prepare(name, issue, continued,
                                       ticket=str(ticket) if stage == "IMPLEMENT" else "",
                                       runtime="codex", conversation_id=None,
                                       worktree=str(worktree))
        self.case.assertIsInstance(snapshot, dict, "public prepare must establish healthy launch")
        self.bindings[key] = snapshot["binding"]
        operation = f"bootstrap-root-{name}-{issue}-{snapshot['binding']['launch_id']}"
        initial_client = f"bootstrap-input-{name}-{issue}-{snapshot['binding']['launch_id']}"
        self.bootstrap_operations[key] = operation
        self.bootstrap_inputs[key] = [{"type": "text", "text": "generic OWN fixture stage prompt"}]
        self.apply(key, {"type": "bootstrap/root-attempted",
                         "observed_revision": self.view(key)["revision"],
                         "root_operation_id": operation, "root_method": "thread/start",
                         "requested_conversation_id": None, "client_message_id": initial_client,
                         "input": self.bootstrap_inputs[key]})
        self.apply(key, {"type": "service", "status": "live"})
        if not unbound:
            self.refine(key)
            self.apply(key, {"type": "inventory", "certainty": "known", "workers": [],
                             "observed_revision": self.view(key)["revision"], "history_checkpoint": {
                                 "launch_id": self.binding(key)["launch_id"], "seeded": True,
                                 "baseline_turns": [], "baseline_workers": [], "seen_completions": [],
                                 "scopes": []}})
            self.apply(key, {"type": "bootstrap/sent",
                             "observed_revision": self.view(key)["revision"],
                             "root_operation_id": operation, "client_message_id": initial_client,
                             "thread_id": self.binding(key)["conversation_id"]})
            self.apply(key, {"type": "turn/started", "thread_id": self.root_id(key),
                             "turn_id": "bootstrap-turn"})
            self.apply(key, {"type": "input/accepted", "client_message_id": initial_client,
                             "turn_id": "bootstrap-turn"})
            self.apply(key, {"type": "turn/completed", "thread_id": self.root_id(key),
                             "turn_id": "bootstrap-turn", "status": "completed"})
            self.apply(key, {"type": "turn/started", "thread_id": self.root_id(key),
                             "turn_id": "active-main"})
        return key

    def refine(self, key):
        root = f"root-{key[0]}-{key[1]}"
        self.apply(key, {"type": "bound", "conversation_id": root,
                         "root_operation_id": self.bootstrap_operations[key]})
        self.bindings[key] = self.view(key)["binding"]

    def signal(self, key, status, **extra):
        task = self.tasks[key]
        stage = "spec" if task.stage.value == "awaiting-spec-review" else task.stage.value
        path = Path(task.worktree) / ".agent" / "stage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stage": stage, "status": status, **extra}))
        return path

    def save(self, key, **changes):
        task = replace(self.tasks[key], **changes)
        self.state.save(self.state_dir, task)
        self.tasks[key] = task
        return task

    def view(self, key):
        return self.client.view(*key)

    def binding(self, key):
        return self.view(key)["binding"]

    def root_id(self, key):
        return self.binding(key)["conversation_id"]

    def apply(self, key, event, *, now=None):
        result = self.client.event(self.binding(key), event, now=now)
        self.case.assertIs(result, True, f"healthy public setup rejected {event['type']}")
        return self.view(key)

    def observe(self, key, event):
        result = self.client.event(self.binding(key), event)
        self.case.assertIsInstance(result, bool, "ordinary event route must return acceptance boolean")
        return self.view(key)

    def compatible(self, key, message="OWN unsupported control format", *, repeated=False):
        (self.observe if repeated else self.apply)(key, {"type": "control/unknown", "message": message})
        return {"kind": "compatibility", "message": message}

    def completion(self, key, label, *, outcome_status="completed"):
        launch = self.binding(key)["launch_id"]
        identity = {"kind": "agent", "thread_id": f"child-{label}-{launch}", "turn_id": f"child-turn-{label}-{launch}"}
        ancestry = [{"thread_id": identity["thread_id"], "parent_thread_id": self.root_id(key),
                     "source_kind": "thread-spawn", "source_parent_thread_id": self.root_id(key), "depth": 1},
                    {"thread_id": self.root_id(key), "parent_thread_id": None,
                     "source_kind": "bound-root", "source_parent_thread_id": None, "depth": 0}]
        outcome = {"status": outcome_status, "messages": [{"item_id": f"message-{label}",
                                                        "text": f"generic OWN outcome {label}"}],
                   "error": {"message": "generic OWN child failure"} if outcome_status == "failed" else None}
        worker = {"identity": identity, "ancestry": ancestry, "thread_status": "active",
                  "active_flags": [], "status": "running", "outcome": None}
        main = self.view(key)["main"]
        if main["status"] == "active":
            self.apply(key, {"type": "turn/completed", "thread_id": self.root_id(key),
                             "turn_id": main["turn_id"], "status": "completed"})
        self.apply(key, {"type": "inventory", "certainty": "known", "workers": [worker],
                         "observed_revision": self.view(key)["revision"]})
        worker = {**worker, "thread_status": "systemError" if outcome_status == "failed" else "idle",
                  "status": outcome_status, "outcome": outcome}
        self.apply(key, {"type": "inventory", "certainty": "known", "workers": [worker],
                         "observed_revision": self.view(key)["revision"]})
        record = {"identity": identity, "outcome": outcome}
        self.case.assertIn(record, self.view(key)["completions"], "OWN public outcome must be available")
        return record

    def propose_result(self, key, batch="D", *, outcome_status="completed"):
        completion = self.completion(key, batch, outcome_status=outcome_status)
        immutable_input = [{"type": "text", "text": json.dumps([completion])}]
        self.apply(key, {"type": "delivery/proposed", "observed_revision": self.view(key)["revision"],
                         "batch_id": batch, "completion_ids": [completion["identity"]], "input": immutable_input})
        self.send_attempt(key, batch)
        return {"kind": "delivery-uncertain", "batch_id": batch}

    def uncertain(self, key, batch="D", *, diagnostic="OWN receipt remains uncertain", outcome_status="completed"):
        identity = self.propose_result(key, batch, outcome_status=outcome_status)
        self.uncertainty(key, batch, diagnostic)
        return identity

    def send_attempt(self, key, batch, suffix="1"):
        if self.view(key)["main"]["status"] != "active":
            self.turn_sequence[key] = self.turn_sequence.get(key, 0) + 1
            self.apply(key, {"type": "turn/started", "thread_id": self.root_id(key),
                             "turn_id": f"active-followup-{self.turn_sequence[key]}"})
        attempt, client = self.attempt_ids(key, batch, suffix)
        self.apply(key, {"type": "delivery/sent", "observed_revision": self.view(key)["revision"],
                         "batch_id": batch, "attempt_id": attempt,
                         "client_message_id": client, "method": "turn/steer",
                         "thread_id": self.root_id(key), "expected_turn_id": self.view(key)["main"]["turn_id"]})

    def attempt_ids(self, key, batch, suffix="1"):
        launch = self.binding(key)["launch_id"]
        return f"attempt-{batch}-{suffix}-{launch}", f"client-{batch}-{suffix}-{launch}"

    def attempt(self, key, batch, suffix="1"):
        attempt, _ = self.attempt_ids(key, batch, suffix)
        return next(item for item in self.delivery(key, batch)["attempts"] if item["attempt_id"] == attempt)

    def uncertainty(self, key, batch, diagnostic, suffix="1", *, repeated=False):
        attempt, client = self.attempt_ids(key, batch, suffix)
        (self.observe if repeated else self.apply)(key, {"type": "delivery/uncertain", "batch_id": batch,
                         "attempt_id": attempt,
                         "client_message_id": client, "message": diagnostic})

    def ack(self, key, batch="D", suffix="1", *, repeated=False):
        attempt, client = self.attempt_ids(key, batch, suffix)
        (self.observe if repeated else self.apply)(key, {"type": "delivery/ack", "batch_id": batch,
                         "attempt_id": attempt,
                         "client_message_id": client,
                         "thread_id": self.root_id(key), "turn_id": self.attempt(key, batch, suffix)["expected_turn_id"]})

    def history_receipt(self, key, batch="D", suffix="1", *, repeated=False):
        delivery = self.delivery(key, batch)
        _, client = self.attempt_ids(key, batch, suffix)
        (self.observe if repeated else self.apply)(key, {"type": "input/history", "client_message_id": client,
                         "thread_id": self.root_id(key), "turn_id": self.attempt(key, batch, suffix)["expected_turn_id"], "item_id": f"receipt-{batch}",
                         "input": delivery["input"], "scan": {"thread_id": self.root_id(key), "turn_id": None,
                         "sort_direction": "asc", "from_cursor": None, "final_cursor": None, "complete": True}})

    def reject(self, key, batch="D", suffix="1"):
        attempt, client = self.attempt_ids(key, batch, suffix)
        self.apply(key, {"type": "delivery/rejected", "batch_id": batch,
                         "attempt_id": attempt,
                         "client_message_id": client,
                         "rejection": {"kind": "expected-active-turn", "code": -32600,
                                       "message": "no active turn to steer"}})

    def delivery(self, key, batch):
        return next(item for item in self.view(key)["deliveries"] if item["batch_id"] == batch)

    def alert(self, key, identity):
        matching = [item for item in self.view(key)["alerts"]
                    if all(item.get(k) == v for k, v in identity.items())]
        self.case.assertEqual(len(matching), 1, "one exact logical condition must be visible")
        return matching[0]

    def claim_event(self, key, identity, phase="pending", revision=None):
        return {"type": "alert/presentation-claimed",
                "observed_revision": self.view(key)["revision"] if revision is None else revision,
                "alert_identity": identity, "phase": phase}

    def present(self, key, notifier=None, *, dry_run=False):
        capability = presentation_capability()
        self.case.assertIsNotNone(capability, "Missing behavior: supported host runtime alert presentation capability")
        if notifier is None:
            notifier = RecordingNotifier()
        returned = capability(self.state_dir, self.targets[key], key[1], notifier, dry_run=dry_run)
        self.case.assertIsNone(returned, "presentation exposes effects, not an authorization token")
        if self.listener.proxy is not None:
            self.case.assertEqual(self.listener.proxy.errors, [], "OWN transport fault must not fail for an unrelated error")
        return notifier

    def audits(self, identity=None, event=None):
        records = self.eventlog.read_tail(self.state_dir, limit=10000)
        result = []
        for record in records:
            if event is not None and record["event"] != event:
                continue
            if record["event"] not in {"runtime-alert-pending", "runtime-alert-resolved"}:
                continue
            detail = json.loads(record["detail"])
            if identity is None or identity_in_detail(detail, identity):
                result.append(record)
        return result

    def snapshot_path(self, key):
        binding = self.binding(key)
        digest = hashlib.sha256(f"{key[0]}\0{key[1]}".encode()).hexdigest()
        return self.state_dir / "runtime" / digest / f"{binding['launch_id']}.json"

    def present_process(self, key, *, audit_action=None):
        self.case.assertIsNotNone(presentation_capability(),
            "Missing behavior: supported host runtime alert presentation capability")
        target = self.targets[key]
        spec = {"state_dir": str(self.state_dir), "target": {
                name: getattr(target, name) for name in ["name", "repo", "clone_path", "worktrees_path",
                "rank_cmd", "project_number", "project_owner", "status_field_id", "status_ready_option_id",
                "status_in_progress_option_id", "gate_cmd"]}, "issue": key[1], "audit_action": audit_action,
                "calls_path": str(self.root / "process-notifier.jsonl")}
        config_path = self.root / "presenter-input.json"
        config_path.write_text(json.dumps(spec))
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("presenter_process.py")),
                                    str(config_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=self.environment)
        self.children.append(process)
        return process

    def process_calls(self):
        path = self.root / "process-notifier.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def evidence(self):
        """Only OWN public observables, never source or host credentials."""
        result = {"tasks": {}, "views": {}, "owned_root": str(self.root)}
        for key in self.tasks:
            label = key[0] + ":" + str(key[1])
            for field, read in [("tasks", lambda: asdict(self.state.load(self.state_dir, *key))),
                                ("views", lambda: self.view(key))]:
                try:
                    result[field][label] = read()
                except Exception as error:
                    result[field][label] = {"observation_error": type(error).__name__, "message": str(error)}
        path = self.root / "pass-output.json"
        if path.exists():
            result["pass"] = json.loads(path.read_text())
        return result

    def background(self, key, *, now=None):
        """Declare a legitimate OWN running child and its exact stopped owner context."""
        now = time.time() if now is None else now
        main = self.view(key)["main"]
        if main["status"] == "unknown":
            last = main["seen_turns"][-1]
            self.apply(key, {"type": "turn/recovered", "thread_id": self.root_id(key),
                             "turn_id": last, "status": "inProgress"}, now=now)
            main = self.view(key)["main"]
        if main["status"] == "active":
            self.apply(key, {"type": "turn/completed", "thread_id": self.root_id(key),
                             "turn_id": main["turn_id"], "status": "completed"}, now=now)
        identity = {"kind": "agent", "thread_id": "running-child-" + self.binding(key)["launch_id"],
                    "turn_id": "running-child-turn"}
        ancestry = [{"thread_id": identity["thread_id"], "parent_thread_id": self.root_id(key),
                     "source_kind": "thread-spawn", "source_parent_thread_id": self.root_id(key), "depth": 1},
                    {"thread_id": self.root_id(key), "parent_thread_id": None,
                     "source_kind": "bound-root", "source_parent_thread_id": None, "depth": 0}]
        self.apply(key, {"type": "inventory", "certainty": "known", "observed_revision": self.view(key)["revision"],
                         "workers": [{"identity": identity, "ancestry": ancestry, "thread_status": "active",
                                      "active_flags": [], "status": "running", "outcome": None}]}, now=now)
        self.case.assertEqual(self.view(key)["main"]["status"], "stopped")
        self.case.assertIsNotNone(self.view(key)["wait"])
        return identity

    def run_pass(self, **options):
        """Real public pass in OWN environment, with declared external objects only."""
        fetched = time.time()
        usage_dir = self.state_dir / "usage"
        usage_dir.mkdir(exist_ok=True)
        for provider in ["openai", "anthropic"]:
            windows = [{"kind": kind, "scope": None, "used": 0.0,
                        "resets_at": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()}
                       for kind, days in [("session", 1), ("weekly", 7)]] if options.get("usage_available") else []
            (usage_dir / f"{provider}.json").write_text(json.dumps({"fetched_at": fetched,
                "usage": {"provider": provider, "source": "oauth" if windows else "unavailable",
                          "fetched_at": fetched, "windows": windows}}))
        targets = {target.name: asdict(target) for target in self.targets.values()}
        result_path = self.root / "pass-output.json"
        spec = {"state_dir": str(self.state_dir), "targets": list(targets.values()),
                "result_path": str(result_path), "continued_stages": {
                    name + ":" + str(issue): task.continued_stage.value
                    for (name, issue), task in self.tasks.items()}, **options}
        path = self.root / "pass-input.json"
        path.write_text(json.dumps(spec))
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("dispatcher_process.py")), str(path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.environment)
        self.children.append(process)
        stdout, stderr = process.communicate(timeout=15)
        self.case.assertEqual(process.returncode, 0, "real run_pass boundary failed: " + stderr + stdout)
        if self.listener.proxy is not None:
            self.case.assertEqual(self.listener.proxy.errors, [])
        return json.loads(result_path.read_text())

    def owned_origin(self, key):
        """Actual local git external artifact boundary; only created after activation."""
        worktree = self.tasks[key].worktree
        origin = self.root / "artifact-origin.git"
        commands = [["git", "init", "--bare", str(origin)],
                    ["git", "-C", worktree, "init", "-b", self.tasks[key].branch],
                    ["git", "-C", worktree, "config", "user.name", "OWN generic fixture"],
                    ["git", "-C", worktree, "config", "user.email", "fixture@example.invalid"],
                    ["git", "-C", worktree, "remote", "add", "origin", str(origin)],
                    ["git", "-C", worktree, "add", ".agent"],
                    ["git", "-C", worktree, "commit", "-m", "generic owned artifact fixture"]]
        for command in commands:
            completed = subprocess.run(command, capture_output=True, text=True, env=self.environment, timeout=10)
            self.case.assertEqual(completed.returncode, 0, completed.stderr)
        return origin


def without_presentation(snapshot):
    result = copy.deepcopy(snapshot)
    result.pop("revision", None)
    for alert in result.get("alerts", []):
        alert.pop("presentation", None)
    return result
