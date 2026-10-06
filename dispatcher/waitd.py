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

from dispatcher.state import (SessionRecord, Stage, load, mark_background,
                              mark_waiting, write_session)


def _load_session_task(state_dir, target: str, issue: int):
    try:
        return load(state_dir, target, issue)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
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
    stage = "spec" if task.stage is Stage.AWAITING_SPEC_REVIEW else task.stage.value
    try:
        write_session(state_dir, target, issue, SessionRecord(session_id, stage))
    except OSError as exc:
        print(f"waitd: cannot record session for {target}#{issue}: {exc}", file=sys.stderr)


def _read_codex_metadata(path: Path, session_id: str) -> dict | None:
    try:
        with path.open(encoding="utf-8") as rollout:
            metadata = json.loads(rollout.readline())
    except (OSError, ValueError):
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


def handle_ping(body: bytes, state_dir) -> None:
    try:
        rec = json.loads(body)
        issue = int(rec["issue"])
        target = str(rec.get("target", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        print(f"waitd: dropping corrupt ping: {body!r}", file=sys.stderr)
        return
    if rec.get("runtime") == "codex":
        _handle_codex_ping(rec, state_dir, target, issue)
        return
    _record_session(rec, state_dir, target, issue)
    bg = rec.get("background_tasks")
    if target and isinstance(bg, list) and bg:
        mark_background(state_dir, target, issue, bg)
    else:
        _mark_waiting(state_dir, target, issue)


class _Server(UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, sock_path, state_dir):
        self.state_dir = state_dir
        super().__init__(str(sock_path), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        handle_ping(self.rfile.read(length), self.server.state_dir)
        self.send_response(200)
        self.end_headers()

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
