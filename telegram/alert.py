"""Failure-alert entrypoint: `python -m telegram.alert <unit>` posts a
Telegram message naming the failed unit and how to recover: re-login for the
keepalive, the unit's journal for anything else. Wired via
OnFailure=agent-ops-alert@%n.service (spec 2026-07-31-auth-resilience).
A unit alerts at most once an hour, so a timer that fails every pass (the
updater runs every two minutes) does not flood the chat. Always exits 0 — an
alert failure must not cascade back into the unit chain (Notifier already
degrades to stderr)."""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

from telegram.notify import Notifier

REPEAT_AFTER_S = 3600


def _muted(unit: str) -> bool:
    """True when `unit` already alerted within the hour; otherwise stamps it."""
    state = Path(os.environ.get("AGENT_OPS_STATE_DIR",
                                Path.home() / "agent-ops-state"))
    marker = state / "alerts" / unit
    try:
        if time.time() - marker.stat().st_mtime < REPEAT_AFTER_S:
            return True
    except OSError:
        pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError as e:  # an unwritable stamp must not swallow the alert
        print(f"alert: cannot stamp {marker}: {e}", file=sys.stderr)
    return False


def main(argv: list[str]) -> int:
    unit = argv[0] if argv else "unknown-unit"
    if _muted(unit):
        return 0
    template = "keepalive_failed" if "keepalive" in unit else "unit_failed"
    Notifier().send(template, unit=unit, host=socket.gethostname())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
