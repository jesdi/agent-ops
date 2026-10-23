"""Long-lived unix-socket listener: worktree Stop hooks curl it whenever a
session stops for input; writes waiting marker → dispatcher parks on next pass.
Accepted v1 noise: it also fires while a human is attached mid-conversation."""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer

from dispatcher.convergence import MIGRATION_LOCK, pass_lock
from dispatcher.state import (SessionRecord, execution_token_matches, load,
                              mark_background, mark_waiting, session_owner,
                              token_issued, write_session)


def _load_session_task(state_dir, target: str, issue: int, repo: str = ""):
    try:
        return load(state_dir, target, issue, repo=repo)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        # TaskState's loader calls .get on operator_request; malformed nested
        # data (for example, []) raises AttributeError instead of TypeError.
        print(f"waitd: cannot read task {target}#{issue} for session recording: {exc}",
              file=sys.stderr)
        return None


def _record_session(rec: dict, state_dir, target: str, issue: int,
                    cwd: str | None = None, repo: str = "") -> None:
    session_id = rec.get("session_id")
    if not target or not isinstance(session_id, str) or not session_id:
        return
    task = _load_session_task(state_dir, target, issue, repo)
    if task is None or (cwd is not None and task.worktree != cwd):
        return
    try:
        write_session(state_dir, target, issue,
                      SessionRecord(session_id, task.continued_stage.value),
                      repo=repo)
    except OSError as exc:
        print(f"waitd: cannot record session for {target}#{issue}: {exc}", file=sys.stderr)


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
    root = Path(state_dir)
    # Each execution has its own codex home (containers.task_home).
    homes = [root / "codex-home", *root.glob("task-homes/*/codex-home")]
    try:
        # The ID is compared literally, never interpolated into a glob pattern.
        for path in (p for h in homes for p in (h / "sessions").glob("**/rollout-*.jsonl")):
            if path.name.endswith(f"-{session_id}.jsonl"):
                return _read_codex_metadata(path, session_id)
    except OSError:
        return None
    return None


def _proven(state_dir, target: str, issue: int, repo: str, token: object,
            legacy: bool, legacy_sender=None) -> bool:
    if legacy:  # a hook from before tokens: tokenless, and its own launch issued none
        return (token is None and not token_issued(state_dir, target, issue, repo)
                and (legacy_sender is None or legacy_sender(target, issue)))
    return execution_token_matches(state_dir, target, issue, repo, token)


def _mark_waiting(state_dir, target: str, issue: int, repo: str = "") -> None:
    if target:
        mark_waiting(state_dir, target, issue, repo=repo)
    else:  # ping from a pre-rename worktree — legacy marker, read via fallback
        p = Path(state_dir) / f"waiting-{issue}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


def _handle_codex_ping(rec: dict, state_dir, target: str, issue: int,
                       repo: str = "") -> None:
    metadata = _codex_metadata(state_dir, rec.get("session_id"))
    source = metadata.get("source") if metadata is not None else None
    if isinstance(source, dict) and "subagent" in source:
        return
    if isinstance(source, str) and isinstance(metadata.get("cwd"), str):
        _record_session(rec, state_dir, target, issue, cwd=metadata["cwd"], repo=repo)
    _mark_waiting(state_dir, target, issue, repo)


def _identity_field(value: object, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid task identity")
    return value


def _ping_identity(rec: dict) -> tuple[str, int, str]:
    """Validate declarations before using them in any state path."""
    # Preserve literal legacy names (including quotes), but never accept the
    # qualified-key delimiter or a path separator as part of a target.
    target = _identity_field(rec.get("target", ""), r"[^~/\x00]*")
    repo = _identity_field(rec.get("repo") or "",
                           r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?")
    if repo and not target:
        raise ValueError("repository requires target")
    return target, int(rec["issue"]), repo


def _ping_owner(rec: dict, state_dir, target: str, issue: int, repo: str,
                legacy_sender) -> str | None:
    """Resolve a legacy launch mapping and prove the sender before any write.

    Socket callers supply a peer check even for unmapped legacy identities.
    Only trusted in-process callers may omit that transport credential.
    """
    legacy = not repo
    if legacy and target:
        repo = session_owner(state_dir, target, issue)
    if not _proven(state_dir, target, issue, repo, rec.get("token"),
                   legacy, legacy_sender):
        return None
    if repo and _load_session_task(state_dir, target, issue, repo) is None:
        return None
    return repo


def handle_ping(body: bytes, state_dir, *, legacy_sender=None) -> None:
    """Process a trusted in-process ping; socket callers must supply the
    transport's legacy sender check, since a destination is not a credential."""
    try:
        rec = json.loads(body)
        if not isinstance(rec, dict):
            raise ValueError("ping must be an object")
        target, issue, repo = _ping_identity(rec)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        print(f"waitd: dropping corrupt ping: {body!r}", file=sys.stderr)
        return
    with pass_lock(state_dir, name=MIGRATION_LOCK):
        _handle_proven_ping(rec, body, state_dir, target, issue, repo, legacy_sender)


def _handle_proven_ping(rec, body, state_dir, target, issue, repo, legacy_sender) -> None:
    repo = _ping_owner(rec, state_dir, target, issue, repo, legacy_sender)
    if repo is None:
        print(f"waitd: dropping ping for unproven task {target}#{issue}",
              file=sys.stderr)
        return
    if rec.get("runtime") == "codex":
        _handle_codex_ping(rec, state_dir, target, issue, repo)
        return
    _record_session(rec, state_dir, target, issue, repo=repo)
    bg = rec.get("background_tasks")
    if target and isinstance(bg, list) and bg:
        mark_background(state_dir, target, issue, bg, repo=repo)
    else:
        _mark_waiting(state_dir, target, issue, repo)


class _Server(UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, sock_path, state_dir):
        self.state_dir = state_dir
        super().__init__(str(sock_path), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        handle_ping(self.rfile.read(length), self.server.state_dir,
                    legacy_sender=lambda target, issue: _legacy_peer_matches(
                        self.connection, target, issue))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

    # BaseHTTPRequestHandler expects a (host, port) client address
    def address_string(self):
        return "unix"


def _legacy_peer_matches(connection, target: str, issue: int) -> bool:
    """Authenticate an old hook using its kernel peer PID and Podman's host
    process list for the verified legacy container. Declarations and the
    caller's UID (shared by rootless containers) cannot prove ownership.
    Platforms without Linux peer credentials fail closed for legacy traffic.
    """
    from dispatcher.sessions import session_name

    try:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                           struct.calcsize("3i"))
        pid, _, _ = struct.unpack("3i", credentials)
        # A target-less hook runs in a pre-rename `task-<issue>` container.
        container = session_name(target, issue) if target else f"task-{issue}"
        result = subprocess.run(
            ["podman", "top", container, "hpid"],
            capture_output=True, text=True, timeout=5, check=True)
        return pid > 0 and str(pid) in result.stdout.split()[1:]
    except (AttributeError, OSError, struct.error, subprocess.SubprocessError):
        return False


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
