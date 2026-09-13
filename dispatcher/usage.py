"""Usage windows, allowance and the per-model gate. Pure: no I/O, no imports
from config/state/web. Adapters (usage_providers.py) produce ProviderUsage;
the dispatcher and the web read model consume Verdicts and views built here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from dispatcher.models import split_model_id

SESSION = timedelta(hours=5)
WEEK = timedelta(days=7)

# Anthropic `limits[].kind` -> (our kind, nominal length). Unknown kinds skip.
_ANTHROPIC_KINDS = {
    "session": ("session", SESSION),
    "weekly_all": ("weekly", WEEK),
    "weekly_scoped": ("weekly", WEEK),
}


@dataclass(frozen=True)
class Window:
    kind: str                 # "session" | "weekly"
    scope: str | None         # model display name ("Fable") or None = all models
    used: float               # 0..1; locked or >=100% -> 1.0
    resets_at: datetime       # aware UTC
    length: timedelta         # nominal, never from the payload


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    source: str               # "oauth" | "ccusage" | "unavailable"
    fetched_at: float
    windows: tuple[Window, ...]


def unavailable(provider: str, fetched_at: float) -> ProviderUsage:
    return ProviderUsage(provider=provider, source="unavailable",
                         fetched_at=fetched_at, windows=())


def _aware(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _used(percent: object, locked_reason: object) -> float | None:
    if locked_reason:
        return 1.0
    if percent is None:
        return None
    return min(1.0, max(0.0, float(percent) / 100.0))


def parse_anthropic(payload: dict) -> tuple[Window, ...]:
    """`limits[]` is the generic structure; the named five_hour/seven_day
    objects are the pre-`limits` shape and are read only in its absence."""
    limits = payload.get("limits")
    if limits is None:
        out = []
        for key, (kind, length) in (("five_hour", ("session", SESSION)),
                                    ("seven_day", ("weekly", WEEK))):
            w = payload.get(key) or {}
            used = _used(w.get("utilization"), w.get("locked_reason"))
            if used is None or not w.get("resets_at"):
                continue
            out.append(Window(kind, None, used, _aware(w["resets_at"]), length))
        return tuple(out)
    out = []
    for entry in limits:
        mapped = _ANTHROPIC_KINDS.get(entry.get("kind"))
        if mapped is None or not entry.get("resets_at"):
            continue
        kind, length = mapped
        used = _used(entry.get("percent"), entry.get("locked_reason"))
        if used is None:
            continue
        scope = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        out.append(Window(kind, scope or None, used, _aware(entry["resets_at"]), length))
    return tuple(out)


def usage_to_json(u: ProviderUsage) -> dict:
    return {"provider": u.provider, "source": u.source, "fetched_at": u.fetched_at,
            "windows": [{"kind": w.kind, "scope": w.scope, "used": w.used,
                         "resets_at": w.resets_at.isoformat(),
                         "length_seconds": w.length.total_seconds()}
                        for w in u.windows]}


def usage_from_json(d: dict) -> ProviderUsage:
    return ProviderUsage(
        provider=d["provider"], source=d["source"], fetched_at=float(d["fetched_at"]),
        windows=tuple(Window(w["kind"], w.get("scope"), float(w["used"]),
                             _aware(w["resets_at"]),
                             timedelta(seconds=float(w["length_seconds"])))
                      for w in d["windows"]))


@dataclass(frozen=True)
class PaceConfig:
    budget_threshold: float
    racing_minutes: int
    racing_threshold: float
    pace_margin: float
    weekend_weight: float
    timezone: str             # IANA name; defines Saturday 00:00 – Monday 00:00


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
    if w.kind == "session":
        racing = minutes_to_reset(w, now) <= cfg.racing_minutes
        return cfg.racing_threshold if racing else cfg.budget_threshold
    start = w.resets_at - w.length
    whole = weighted_hours(start, w.resets_at, cfg.timezone, cfg.weekend_weight)
    done = weighted_hours(start, min(now, w.resets_at), cfg.timezone, cfg.weekend_weight)
    elapsed = done / whole if whole > 0 else 1.0
    return min(1.0, elapsed + cfg.pace_margin)


@dataclass(frozen=True)
class Verdict:
    admitted: bool
    provider: str
    window: Window | None     # binding window; None when the provider is unavailable
    allowance: float
    headroom: float           # allowance - used on the binding window
    reason: str               # "ok" | "over-pace" | "over-threshold" | "unavailable"


def considered(windows: Iterable[Window], bare_model: str) -> list[Window]:
    """Unscoped windows always; a scoped one only when its display name is
    a substring of the model id."""
    lower = bare_model.lower()
    return [w for w in windows if w.scope is None or w.scope.lower() in lower]


def judge(provider: str, usage: ProviderUsage | None, windows: Iterable[Window],
          now: datetime, cfg: PaceConfig) -> Verdict:
    """Pure verdict over the given windows. Task 8 calls this directly over all
    of a provider's windows to name the binding window regardless of model scope."""
    if usage is None or usage.source == "unavailable":
        return Verdict(False, provider, None, 0.0, 0.0, "unavailable")
    scored = [(a - w.used, a, w)
              for w in windows
              for a in (allowance(w, now, cfg),)]
    if not scored:
        return Verdict(True, provider, None, 1.0, 1.0, "ok")
    headroom, allowed, binding = min(scored, key=lambda s: s[0])
    if headroom > 0:
        return Verdict(True, provider, binding, allowed, headroom, "ok")
    reason = "over-threshold" if binding.kind == "session" else "over-pace"
    return Verdict(False, provider, binding, allowed, headroom, reason)


def admits(usages: Mapping[str, ProviderUsage], model_id: str,
           now: datetime, cfg: PaceConfig) -> Verdict:
    provider, bare = split_model_id(model_id)
    u = usages.get(provider)
    return judge(provider, u, considered(u.windows, bare) if u else (), now, cfg)


def window_label(w: Window) -> str:
    return f"{w.kind}·{w.scope}" if w.scope else w.kind


def _duration(minutes: float) -> str:
    m = int(minutes)
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m}m" if h else f"{m}m"


def verdict_note(v: Verdict, now: datetime) -> str:
    if v.window is None:
        return (f"{v.provider}: usage unavailable" if v.reason == "unavailable"
                else f"{v.provider}: no usage windows reported")
    w = v.window
    return (f"{v.provider} {window_label(w)}: {w.used:.0%} used, "
            f"allowance {v.allowance:.0%}, headroom {v.headroom * 100:.0f} pts, "
            f"resets in {_duration(minutes_to_reset(w, now))}")
