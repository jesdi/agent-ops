"""Long-lived unix-socket listener: worktree Stop hooks curl it whenever a
session stops for input; it forwards "task #N is waiting" to Telegram.
Accepted v1 noise: it also fires while a human is attached mid-conversation."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer

from telegram.notify import Notifier


def handle_ping(body: bytes, notifier) -> None:
    try:
        issue = int(json.loads(body)["issue"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        print(f"waitd: dropping corrupt ping: {body!r}", file=sys.stderr)
        return
    notifier.send("waiting", issue=issue)


class _Server(UnixStreamServer):
    allow_reuse_address = True

    def __init__(self, sock_path, notifier):
        self.notifier = notifier
        super().__init__(str(sock_path), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        handle_ping(self.rfile.read(length), self.server.notifier)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

    # BaseHTTPRequestHandler expects a (host, port) client address
    def address_string(self):
        return "unix"


def serve(sock_path: str | Path, notifier) -> None:
    p = Path(sock_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        p.unlink()
    _Server(p, notifier).serve_forever()


def main() -> None:
    state_dir = Path(os.environ.get("AGENT_OPS_STATE_DIR",
                                    Path.home() / "agent-ops-state"))
    serve(state_dir / "wait.sock", Notifier())


if __name__ == "__main__":
    main()
