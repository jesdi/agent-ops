"""Failure-alert entrypoint: `python -m telegram.alert <unit>` posts a
Telegram message naming the failed unit plus the re-login recovery command.
Wired via OnFailure=agent-ops-alert@%n.service (spec
2026-07-31-auth-resilience). Always exits 0 — an alert failure must not
cascade back into the unit chain (Notifier already degrades to stderr)."""
from __future__ import annotations

import socket
import sys

from telegram.notify import Notifier


def main(argv: list[str]) -> int:
    unit = argv[0] if argv else "unknown-unit"
    Notifier().send("unit_failed", unit=unit, host=socket.gethostname())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
