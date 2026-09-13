"""Usage windows, allowance and the per-model gate. Pure: no I/O, no imports
from config/state/web. Adapters (usage_providers.py) produce ProviderUsage;
the dispatcher and the web read model consume Verdicts and views built here."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
