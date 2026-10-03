"""The order a routed list's entries are tried in. Pure: the usage gate
(dispatcher.usage.admits) still decides which of them may run."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from dispatcher.models import Entry, Order
from dispatcher.usage import (PaceConfig, ProviderUsage, _applies, readings,
                              required_pace, session_bound)

AUTO = "auto"


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
    """Auto, the only mode yet: entries of a session-bound provider first;
    in that group and in the rest, highest required pace first, entries
    without one last, exact ties in written order."""
    bound = {name for name, u in usages.items() if session_bound(u, now, pace)}

    def rank(e: Entry) -> tuple[bool, bool, float]:
        p = _entry_pace(usages, e, now, pace)
        return (e.provider not in bound, p is None, 0.0 if p is None else -p)
    return lambda entries: tuple(sorted(entries, key=rank))
