"""Unix HTTP transport; only the host credential authorizes launch preparation."""
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import socket

from dispatcher.runtime_snapshots import read_current, task_path


def credential_path(state_dir):
    return Path(state_dir) / "runtime-host-token"


def ensure_credential(state_dir):
    path = credential_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_hex(32))
        stream.flush()
        os.fsync(stream.fileno())


def authorized(state_dir, token):
    try:
        expected = credential_path(state_dir).read_text()
        return bool(expected) and hmac.compare_digest(expected, token or "")
    except OSError:
        return False


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, state_dir):
        super().__init__("localhost", timeout=5)
        self.path = str(Path(state_dir) / "wait" / "wait.sock")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class RuntimeClient:
    """Host mutations go through the sole writer; reads use atomic snapshots."""
    def __init__(self, state_dir):
        self.state_dir = state_dir

    def _post(self, route, payload):
        connection = UnixHTTP(self.state_dir)
        headers = {"Content-Type": "application/json",
                   "X-Runtime-Host": credential_path(self.state_dir).read_text()}
        try:
            connection.request("POST", route, json.dumps(payload, allow_nan=False), headers)
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise RuntimeError(f"runtime listener rejected {route}: {response.status}")
            return json.loads(data)
        finally:
            connection.close()

    def prepare(self, target, issue, stage, *, ticket="", runtime="codex",
                conversation_id=None, worktree=""):
        return self._post("/runtime/prepare", dict(target=target, issue=issue, stage=stage,
                          ticket=ticket, runtime=runtime, conversation_id=conversation_id,
                          worktree=worktree))

    def view(self, target, issue, launch_id=None):
        return read_current(task_path(self.state_dir, target, issue), target, issue, launch_id)

    def event(self, binding, event, *, now=None):
        return self._post("/runtime/event", dict(binding=binding, event=event, now=now))


def dispatch(state_dir, route, payload, token):
    from dispatcher.runtime_control import RuntimeControl
    control = RuntimeControl(state_dir)
    host = authorized(state_dir, token)
    if route == "/runtime/prepare":
        if not host:
            return 403, None
        return 200, control.prepare(**payload)
    if route == "/runtime/view":
        if not host and not payload.get("launch_id"):
            return 403, None
        return 200, control.view(**payload)
    if route == "/runtime/event":
        return 200, control.event(**payload)
    return 404, None
