"""Usage adapters: one per provider, each returning a ProviderUsage, and the
per-provider cache. I/O and every provider's payload shape live here; the
arithmetic lives in usage.py. Research feeding the Anthropic adapter:
portfolio_eval#155; payload verified 2026-09-13 (tests/fixtures)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from dispatcher.config import Config, referenced_providers
from dispatcher.usage import ProviderUsage, Window, WindowKind, unavailable

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Mandatory per community findings: requests without this UA get 429'd.
USER_AGENT = "claude-code/2.0.0"
MIN_POLL_SECONDS = 180
HOST_CREDENTIALS = "~/.claude/.credentials.json"


class UsageFetchError(Exception):
    pass


class UsageAdapter(Protocol):
    name: str

    def fetch(self, state_dir: Path, *, now: Callable[[], float] = time.time) -> ProviderUsage: ...


# ---- Anthropic payload ----------------------------------------------------

# Anthropic `limits[].kind` -> our kind. Unknown kinds are skipped.
_ANTHROPIC_KINDS = {
    "session": WindowKind.SESSION,
    "weekly_all": WindowKind.WEEKLY,
    "weekly_scoped": WindowKind.WEEKLY,
}
# The pre-`limits` named objects, and the limits kind each one duplicates.
_LEGACY_KINDS = (("five_hour", "session"), ("seven_day", "weekly_all"))


def _aware(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _used(percent: object, locked_reason: object) -> float | None:
    """A percent as a 0..1 fraction; locked counts as fully used."""
    if locked_reason:
        return 1.0
    if percent is None:
        return None
    return min(1.0, max(0.0, float(percent) / 100.0))


def _legacy_limits(payload: dict) -> list[dict]:
    """The named five_hour/seven_day objects, reshaped into limits entries."""
    entries = []
    for key, kind in _LEGACY_KINDS:
        w = payload.get(key)
        if isinstance(w, dict):
            entries.append({"kind": kind, "percent": w.get("utilization"),
                            "locked_reason": w.get("locked_reason"),
                            "resets_at": w.get("resets_at")})
    return entries


def _scope(raw: object) -> str | None:
    """The scoped model's display name. A scope of unexpected shape reads as
    no scope, so the window counts against every model — the closed side."""
    model = raw.get("model") if isinstance(raw, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return name or None


def _window(entry: object) -> Window | None:
    """One limits entry, or None when it is not a window this box can read:
    not an object, an unknown kind, no reset, or no percent."""
    if not isinstance(entry, dict):
        return None
    kind = _ANTHROPIC_KINDS.get(entry.get("kind"))
    used = _used(entry.get("percent"), entry.get("locked_reason"))
    if kind is None or used is None or not entry.get("resets_at"):
        return None
    return Window(kind, _scope(entry.get("scope")), used, _aware(entry["resets_at"]))


def parse_anthropic(payload: dict) -> tuple[Window, ...]:
    """`limits[]` is the generic structure; the named five_hour/seven_day
    objects duplicate its first two entries and are read only without it."""
    entries = payload.get("limits") or _legacy_limits(payload)
    return tuple(w for w in map(_window, entries) if w is not None)


# ---- Anthropic adapter ----------------------------------------------------

def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise UsageFetchError(str(e)) from e


def _ccusage_json() -> dict | None:
    try:
        out = subprocess.run(["ccusage", "blocks", "--json"],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _parse_ccusage(data: dict, now: float) -> tuple[Window, ...]:
    """ccusage knows the session window only; the weekly windows are absent,
    which the gate treats as passing and the console labels unknown."""
    active = [b for b in data.get("blocks", []) if b.get("isActive")]
    if not active:
        return ()
    b = active[0]
    remaining = float((b.get("projection") or {}).get("remainingMinutes", 0))
    resets = datetime.fromtimestamp(now, timezone.utc) + timedelta(minutes=remaining)
    return (Window(WindowKind.SESSION, None, _used(b.get("percentUsed") or 0.0, None), resets),)


def _read_token(credentials_path: str | Path) -> str | None:
    p = Path(credentials_path).expanduser()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _token_from_op() -> str | None:
    """Long-lived setup-token, read from 1P at fetch time — never persisted
    on the box. Only attempted when the caller already carries op service
    auth (dispatcher and triage units remap it into env); web has no op
    plumbing and must not shell out to an op that can only hang or prompt.
    The ≥180s fetch cache bounds the 1P call rate."""
    if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return None
    try:
        r = subprocess.run(
            [os.environ.get("AGENT_OPS_OP", "op"), "read",
             "op://agent-ops/agent-ops-claude/CLAUDE_CODE_OAUTH_TOKEN"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _resolve_credentials(state_dir: str | Path,
                         credentials_path: str | Path | None) -> str | Path:
    # The claude-home store (mounted into every session container, renewed
    # by the keepalive) is the only one the fleet keeps fresh; the host's
    # ~/.claude lapses ~8h after the last host-side claude run and would
    # take the budget check dark with it. Prefer claude-home, fall back to
    # the host store for dev machines without one.
    if credentials_path is not None:
        return credentials_path
    claude_home = Path(state_dir) / "claude-home" / ".credentials.json"
    return claude_home if claude_home.exists() else HOST_CREDENTIALS


class AnthropicUsage:
    name = "anthropic"

    def __init__(self, credentials_path: str | Path | None = None):
        self._credentials_path = credentials_path

    def fetch(self, state_dir: Path, *, now: Callable[[], float] = time.time) -> ProviderUsage:
        """OAuth endpoint → ccusage → unavailable. Token preference: explicit
        env override, the box's long-lived setup-token read from 1P at fetch
        time, then the shared OAuth store (kept as a live fallback: the usage
        endpoint is unofficial and may reject the static token)."""
        candidates = [t for t in (
            os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
            _token_from_op(),
            _read_token(_resolve_credentials(state_dir, self._credentials_path)),
        ) if t]
        at = now()
        for token in candidates:
            try:
                windows = parse_anthropic(_http_get_json(
                    USAGE_URL, {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}))
            except (UsageFetchError, KeyError, ValueError, TypeError):
                continue  # next token, then ccusage; the cache spaces passes ≥180s apart
            # A reading with no windows would admit every model: fail closed
            # exactly like a rejected token.
            if windows:
                return ProviderUsage(self.name, "oauth", at, windows)
        cc = _ccusage_json()
        windows = _parse_ccusage(cc, at) if cc is not None else ()
        if windows:
            return ProviderUsage(self.name, "ccusage", at, windows)
        return unavailable(self.name, at)


ADAPTERS: dict[str, UsageAdapter] = {"anthropic": AnthropicUsage()}


# ---- cache + fan-out -------------------------------------------------------

def usage_to_json(u: ProviderUsage) -> dict:
    return {"provider": u.provider, "source": u.source, "fetched_at": u.fetched_at,
            "windows": [{"kind": w.kind.value, "scope": w.scope, "used": w.used,
                         "resets_at": w.resets_at.isoformat()}
                        for w in u.windows]}


def usage_from_json(d: dict) -> ProviderUsage:
    """Raises on anything it cannot read back; the cache then refetches."""
    return ProviderUsage(
        provider=d["provider"], source=d["source"], fetched_at=float(d["fetched_at"]),
        windows=tuple(Window(WindowKind(w["kind"]), w.get("scope"), float(w["used"]),
                             _aware(w["resets_at"]))
                      for w in d["windows"]))


def cache_path(state_dir: str | Path, provider: str) -> Path:
    return Path(state_dir) / "usage" / f"{provider}.json"


def _write_atomic(path: Path, doc: dict) -> None:
    """The web process and the dispatcher both write the cache: write a
    sibling temp file and rename it over, so a reader never sees a torn one.
    mkstemp creates 0600 and the two units may run as different users, so
    the file is opened up to 0644 before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _cached(cp: Path, at: float) -> ProviderUsage | None:
    """The cached reading if it is fresh and usable; None is a miss. A file
    this unit cannot read, or cannot parse, is a miss, never a raise. So is
    a readable reading with no windows (written before readings failed
    closed): served, it would admit every model until it aged out."""
    try:
        cached = json.loads(cp.read_text())
        if not 0 <= at - cached["fetched_at"] < MIN_POLL_SECONDS:
            return None
        u = usage_from_json(cached["usage"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return u if u.windows or u.source == "unavailable" else None


def fetch_provider(name: str, state_dir: str | Path, *,
                   now: Callable[[], float] = time.time,
                   adapters: dict[str, UsageAdapter] = ADAPTERS) -> ProviderUsage:
    """Cached ≥ MIN_POLL_SECONDS per provider; only readable results are
    cached, so an outage never masks a recovery for 180s. One clock reading
    serves the whole fetch: the cache age, the reading and the cache stamp."""
    at = now()
    cp = cache_path(state_dir, name)
    hit = _cached(cp, at)
    if hit is not None:
        return hit
    adapter = adapters.get(name)
    if adapter is None:
        return unavailable(name, at)
    try:
        u = adapter.fetch(Path(state_dir), now=lambda: at)
    except Exception:
        # One broken adapter (a payload shape nobody anticipated) must fail
        # its own provider closed, never crash the pass or /api/board.
        log.exception("usage adapter %r failed", name)
        return unavailable(name, at)
    if u.source != "unavailable":
        _write_atomic(cp, {"fetched_at": at, "usage": usage_to_json(u)})
    return u


def fetch_all(cfg: Config, *, now: Callable[[], float] = time.time,
              adapters: dict[str, UsageAdapter] = ADAPTERS) -> dict[str, ProviderUsage]:
    return {p: fetch_provider(p, cfg.state_dir, now=now, adapters=adapters)
            for p in sorted(referenced_providers(cfg))}
