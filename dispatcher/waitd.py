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

from dispatcher.state import mark_waiting


def handle_ping(body: bytes, state_dir) -> None:
    try:
        rec = json.loads(body)
        issue = int(rec["issue"])
        target = str(rec.get("target", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        print(f"waitd: dropping corrupt ping: {body!r}", file=sys.stderr)
        return
    if target:
        mark_waiting(state_dir, target, issue)
    else:  # ping from a pre-rename worktree — legacy marker, read via fallback
        p = Path(state_dir) / f"waiting-{issue}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


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
