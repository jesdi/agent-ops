"""The order a routed list's entries are tried in. Pure: the usage gate
(dispatcher.usage.admits) still decides which of them may run."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping

from dispatcher.models import Entry, Order
from dispatcher.usage import (PaceConfig, ProviderUsage, _applies, readings,
                              required_pace)

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
    """Auto, the only mode yet: highest required pace first, entries without
    one last, ties in written order. Paces are compared at the one decimal
    the console shows, so reset times a moment apart still tie."""
    def rank(e: Entry) -> tuple[bool, float]:
        p = _entry_pace(usages, e, now, pace)
        return (p is None, 0.0 if p is None else -round(p, 1))
    return lambda entries: tuple(sorted(entries, key=rank))
