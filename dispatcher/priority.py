"""The order a routed list's entries are tried in, and the stored priority
mode that picks it (`<state_dir>/provider-priority.json`, read and written
here). `order` itself is pure; the usage gate (dispatcher.usage.admits) still
decides which of the entries may run."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Collection, Mapping

from dispatcher.models import Entry, Order
from dispatcher.state import write_json_atomic
from dispatcher.usage import (PaceConfig, ProviderUsage, _applies, readings,
                              required_pace, session_bound)

AUTO = "auto"
FILE = "provider-priority.json"


def load(state_dir: str | Path, routed: Collection[str]) -> str:
    """The stored priority mode. Never fails and never rewrites: a missing or
    unreadable file, a `mode` that is not a string, or a provider `routed`
    does not hold all read as auto, so a provider routed again later brings
    its stored mode back."""
    try:
        mode = json.loads((Path(state_dir) / FILE).read_text())["mode"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return AUTO
    return mode if isinstance(mode, str) and mode in routed else AUTO


def save(state_dir: str | Path, mode: str, *, actor: str, now: datetime) -> None:
    """The console's write; the dispatcher, maybe another user, reads it."""
    write_json_atomic(Path(state_dir) / FILE, {
        "mode": mode, "set_by": actor,
        "set_at": now.astimezone(timezone.utc).isoformat()})


def _entry_pace(usages: Mapping[str, ProviderUsage], entry: Entry,
                now: datetime, pace: PaceConfig) -> float | None:
    """The lowest required pace among the weekly windows the entry's model
    draws on (the gate's own scope rule), or None when it has none: provider
    missing or unavailable, no weekly window, or none with time left."""
    usage = usages.get(entry.provider)
    if usage is None:
        return None
    paces = [p for r in readings(usage, now, pace) if _applies(r, entry.model)
             and (p := required_pace(r.window, now, pace)) is not None]
    return min(paces, default=None)


def order(mode: str, usages: Mapping[str, ProviderUsage], now: datetime,
          pace: PaceConfig) -> Order:
    """A provider's name: its entries first, the rest after, each group in
    written order; the session-bound rule is not applied. Auto: entries of a
    session-bound provider first; in that group and in the rest, highest
    required pace first, entries without one last, exact ties in written
    order."""
    if mode != AUTO:
        return lambda entries: tuple(sorted(entries, key=lambda e: e.provider != mode))
    bound = {name for name, u in usages.items() if session_bound(u, now, pace)}

    def rank(e: Entry) -> tuple[bool, bool, float]:
        p = _entry_pace(usages, e, now, pace)
        return (e.provider not in bound, p is None, 0.0 if p is None else -p)
    return lambda entries: tuple(sorted(entries, key=rank))
