"""Long-lived unix-socket listener: worktree Stop hooks curl it whenever a
session stops for input; writes waiting marker → dispatcher parks on next pass.
Accepted v1 noise: it also fires while a human is attached mid-conversation."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer

from dispatcher.runtime_control import RuntimeControl
from dispatcher.runtime_http import dispatch, ensure_credential
from dispatcher.state import (SessionRecord, load, mark_background,
                              mark_waiting, write_session)


def _load_session_task(state_dir, target: str, issue: int):
    try:
        return load(state_dir, target, issue)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        # TaskState's loader calls .get on operator_request; malformed nested
        # data (for example, []) raises AttributeError instead of TypeError.
        print(f"waitd: cannot read task {target}#{issue} for session recording: {exc}",
              file=sys.stderr)
        return None


def _record_session(rec: dict, state_dir, target: str, issue: int,
                    cwd: str | None = None) -> None:
    session_id = rec.get("session_id")
    if not target or not isinstance(session_id, str) or not session_id:
        return
    task = _load_session_task(state_dir, target, issue)
    if task is None or (cwd is not None and task.worktree != cwd):
        return
    try:
        write_session(state_dir, target, issue,
                      SessionRecord(session_id, task.continued_stage.value))
    except OSError as exc:
        print(f"waitd: cannot record session for {target}#{issue}: {exc}", file=sys.stderr)


def record_control_session(state_dir, binding):
    """Only an accepted current completion may persist this launch's stage."""
    write_session(state_dir, binding["target"], binding["issue"],
                  SessionRecord(binding["conversation_id"], binding["stage"]))


def _read_codex_metadata(path: Path, session_id: str) -> dict | None:
    try:
        with path.open(encoding="utf-8") as rollout:
            metadata = json.loads(rollout.readline())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(metadata, dict) or metadata.get("type") != "session_meta":
        return None
    payload = metadata.get("payload")
    if not isinstance(payload, dict) or payload.get("id") != session_id:
        return None
    return payload


def _codex_metadata(state_dir, session_id) -> dict | None:
    if not isinstance(session_id, str) or not session_id:
        return None
    sessions = Path(state_dir) / "codex-home" / "sessions"
    try:
        # The ID is compared literally, never interpolated into a glob pattern.
        for path in sessions.glob("**/rollout-*.jsonl"):
            if path.name.endswith(f"-{session_id}.jsonl"):
                return _read_codex_metadata(path, session_id)
    except OSError:
        return None
    return None


def _mark_waiting(state_dir, target: str, issue: int) -> None:
    if target:
        mark_waiting(state_dir, target, issue)
    else:  # ping from a pre-rename worktree — legacy marker, read via fallback
        p = Path(state_dir) / f"waiting-{issue}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


def _handle_codex_ping(rec: dict, state_dir, target: str, issue: int) -> None:
    metadata = _codex_metadata(state_dir, rec.get("session_id"))
    source = metadata.get("source") if metadata is not None else None
    if isinstance(source, dict) and "subagent" in source:
        return
    if isinstance(source, str) and isinstance(metadata.get("cwd"), str):
        _record_session(rec, state_dir, target, issue, cwd=metadata["cwd"])
    _mark_waiting(state_dir, target, issue)


def handle_ping(body: bytes, state_dir) -> bool | None:
    try:
        rec = json.loads(body)
        issue = int(rec["issue"])
        target = str(rec.get("target", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError, RecursionError):
        print(f"waitd: dropping corrupt ping: {body!r}", file=sys.stderr)
        return
    # Prepared launches accept only their authoritative bound lifecycle.
    # Keep the legacy recording/marker path for sessions not yet migrated.
    control = RuntimeControl(state_dir)
    snapshot = control.view(target, issue)
    if snapshot is not None:
        return _native_ping(control, snapshot, rec, state_dir)
    if rec.get("runtime") == "codex":
        _handle_codex_ping(rec, state_dir, target, issue)
        return
    _record_session(rec, state_dir, target, issue)
    bg = rec.get("background_tasks")
    if target and isinstance(bg, list) and bg:
        mark_background(state_dir, target, issue, bg)
    else:
        _mark_waiting(state_dir, target, issue)


def _native_ping(control, snapshot, rec, state_dir):
    binding = snapshot["binding"]
    if binding.get("runtime") != "claude" or rec.get("agent_id"):
        return False
    identity = {"launch_id": "launch_id", "conversation_id": "session_id",
                "stage": "stage", "ticket": "ticket"}
    if any(binding[key] != rec.get(native) for key, native in identity.items()):
        return False
    event = _native_event(rec)
    if event is None or not control.event(binding, event):
        return False
    if rec["hook_event_name"] == "Stop":
        write_session(state_dir, binding["target"], binding["issue"],
                      SessionRecord(binding["conversation_id"], binding["stage"]))
    return True


def _native_event(rec):
    name = rec.get("hook_event_name")
    if name == "SessionStart":
        return {"type": "service", "status": "live"}
    types = {"UserPromptSubmit": "turn/started", "Stop": "turn/completed"}
    if not isinstance(name, str) or name not in types:
        return None
    return {"type": types[name], "thread_id": rec.get("session_id"),
            "turn_id": rec.get("prompt_id"), "status": "completed",
            "background_tasks": rec.get("background_tasks")}


class _Server(UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, sock_path, state_dir):
        self.state_dir = state_dir
        super().__init__(str(sock_path), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 1024 * 1024:
                raise ValueError("invalid request length")
            body = self.rfile.read(length)
            status, result = self._route(body)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            status, result = 400, None
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _route(self, body):
        if self.path == "/waiting":
            return 200, handle_ping(body, self.server.state_dir)
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return 400, None
        return dispatch(self.server.state_dir, self.path, payload,
                        self.headers.get("X-Runtime-Host"))

    def log_message(self, *args):
        pass

    # BaseHTTPRequestHandler expects a (host, port) client address
    def address_string(self):
        return "unix"


def sock_path(state_dir: str | Path) -> Path:
    """Socket lives in a dedicated subdir so session containers can
    bind-mount just that dir: a file bind goes stale when waitd recreates
    the socket, and mounting the whole state dir would expose secrets
    (op-token.env) to every session."""
    return Path(state_dir) / "wait" / "wait.sock"


def serve(path: str | Path, state_dir: str | Path) -> None:
    ensure_credential(state_dir)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    _Server(p, state_dir).serve_forever()


def main() -> None:
    state_dir = Path(os.environ.get("AGENT_OPS_STATE_DIR",
                                    Path.home() / "agent-ops-state"))
    serve(sock_path(state_dir), state_dir)


if __name__ == "__main__":
    main()
