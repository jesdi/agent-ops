"""Usage windows, allowance and the per-model gate. Pure: no I/O, no imports
from config/state/web. Adapters (usage_providers.py) produce ProviderUsage;
the dispatcher and the web read model consume the Readings and Verdicts
built here, so nothing outside this module computes allowance or headroom."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from dispatcher.models import split_model_id

Source = Literal["oauth", "ccusage", "unavailable"]
Reason = Literal["ok", "over-pace", "over-threshold", "unavailable"]


class WindowKind(StrEnum):
    SESSION = "session"
    WEEKLY = "weekly"

    @property
    def length(self) -> timedelta:
        """Nominal length — never read from a payload."""
        return timedelta(hours=5) if self is WindowKind.SESSION else timedelta(days=7)

    @property
    def deny_reason(self) -> Reason:
        """The session window keeps a fixed threshold; a weekly one a pace."""
        return "over-threshold" if self is WindowKind.SESSION else "over-pace"


@dataclass(frozen=True)
class Window:
    kind: WindowKind
    scope: str | None         # model display name ("Fable") or None = all models
    used: float               # 0..1; locked or >=100% -> 1.0
    resets_at: datetime       # aware UTC

    @property
    def length(self) -> timedelta:
        return self.kind.length


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    source: Source
    fetched_at: float
    windows: tuple[Window, ...]


def unavailable(provider: str, fetched_at: float) -> ProviderUsage:
    return ProviderUsage(provider=provider, source="unavailable",
                         fetched_at=fetched_at, windows=())


@dataclass(frozen=True)
class PaceConfig:
    budget_threshold: float = 0.8   # session window: the ceiling far from its reset
    racing_minutes: int = 30        # session window: this close to the reset...
    racing_threshold: float = 0.95  # ...the ceiling relaxes to this
    pace_margin: float = 0.10       # how far ahead of the weighted schedule the box may run
    weekend_weight: float = 0.5     # a weekend hour counts this much of a weekday hour
    timezone: str = "UTC"           # IANA name; defines Saturday 00:00 – Monday 00:00


def weighted_hours(start: datetime, end: datetime, tz: str,
                   weekend_weight: float) -> float:
    """Hours in [start, end) with Saturday/Sunday (local calendar days in
    `tz`) counted at weekend_weight. Integrated per local day so DST days
    weigh their true 23 or 25 hours; at most 8 iterations for a week."""
    zone = ZoneInfo(tz)
    total, t = 0.0, start
    while t < end:
        local = t.astimezone(zone)
        next_midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        seg_end = min(end, next_midnight.astimezone(timezone.utc))
        weight = weekend_weight if local.weekday() >= 5 else 1.0
        total += weight * (seg_end - t).total_seconds() / 3600.0
        t = seg_end
    return total


def minutes_to_reset(w: Window, now: datetime) -> float:
    return max(0.0, (w.resets_at - now).total_seconds() / 60.0)


def allowance(w: Window, now: datetime, cfg: PaceConfig) -> float:
    """What fraction of the window the box may have spent by `now`."""
    if w.kind is WindowKind.SESSION:
        racing = minutes_to_reset(w, now) <= cfg.racing_minutes
        return cfg.racing_threshold if racing else cfg.budget_threshold
    start = w.resets_at - w.length
    whole = weighted_hours(start, w.resets_at, cfg.timezone, cfg.weekend_weight)
    done = weighted_hours(start, min(now, w.resets_at), cfg.timezone, cfg.weekend_weight)
    elapsed = done / whole if whole > 0 else 1.0
    return min(1.0, elapsed + cfg.pace_margin)


@dataclass(frozen=True)
class Reading:
    """One window judged at a moment: what the box may have spent by then,
    and the headroom (allowance - used) left — the gate's actual input."""
    window: Window
    allowance: float
    headroom: float


def _reading(w: Window, now: datetime, cfg: PaceConfig) -> Reading:
    allowed = allowance(w, now, cfg)
    return Reading(w, allowed, allowed - w.used)


def readings(usage: ProviderUsage, now: datetime,
             cfg: PaceConfig) -> tuple[Reading, ...]:
    """One Reading per window; none for an unavailable provider, whose
    windows (if a reading carried any) are not to be trusted."""
    if usage.source == "unavailable":
        return ()
    return tuple(_reading(w, now, cfg) for w in usage.windows)


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    provider: str
    # The considered reading with the least headroom. None when the provider
    # is unavailable, or when no window it reported applies to the model.
    binding: Reading | None
    reason: Reason


def _applies(r: Reading, bare_model: str) -> bool:
    """Unscoped windows always; a scoped one only when its display name is
    a case-insensitive substring of the model id."""
    scope = r.window.scope
    return scope is None or scope.lower() in bare_model.lower()


def admits(usages: Mapping[str, ProviderUsage], model_id: str,
           now: datetime, cfg: PaceConfig) -> Verdict:
    """Fail closed on a missing or unavailable provider; otherwise admitted
    iff every window the model draws on has headroom above zero. A window
    the provider did not report is simply absent and passes."""
    provider, bare = split_model_id(model_id)
    usage = usages.get(provider)
    if usage is None or usage.source == "unavailable":
        return Verdict(False, provider, None, "unavailable")
    considered = [r for r in readings(usage, now, cfg) if _applies(r, bare)]
    binding = min(considered, key=lambda r: r.headroom, default=None)
    if binding is None or binding.headroom > 0:
        return Verdict(True, provider, binding, "ok")
    return Verdict(False, provider, binding, binding.window.kind.deny_reason)


def window_label(w: Window) -> str:
    kind = "session" if w.kind is WindowKind.SESSION else "week"
    return f"{kind}·{w.scope}" if w.scope else kind


def points(fraction: float) -> str:
    """A headroom fraction as whole percentage points: a true minus sign,
    and "1 pt" singular."""
    n = round(fraction * 100)
    return f"{'−' if n < 0 else ''}{abs(n)} {'pt' if abs(n) == 1 else 'pts'}"


def _duration(minutes: float) -> str:
    m = int(minutes)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m}m" if h else f"{m}m"


def verdict_note(v: Verdict, now: datetime) -> str:
    b = v.binding
    if b is None:
        return (f"{v.provider}: usage unavailable" if v.reason == "unavailable"
                else f"{v.provider}: no usage windows reported")
    w = b.window
    return (f"{v.provider} {window_label(w)}: {w.used:.0%} used, "
            f"allowance {b.allowance:.0%}, headroom {points(b.headroom)}, "
            f"resets in {_duration(minutes_to_reset(w, now))}")
