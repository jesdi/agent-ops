"""Usage adapters: one per provider, each returning a ProviderUsage. I/O
lives here; the arithmetic lives in usage.py. Research feeding the Anthropic
adapter: portfolio_eval#155; payload verified 2026-09-13 (tests/fixtures)."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from dispatcher.config import Config, referenced_providers
from dispatcher.usage import (SESSION, ProviderUsage, Window, parse_anthropic,
                              unavailable, usage_from_json, usage_to_json)

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


# ---- Anthropic ------------------------------------------------------------

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


def _parse_ccusage(data: dict) -> tuple[Window, ...]:
    """ccusage knows the session window only; the weekly windows are absent,
    which the gate treats as passing and the console labels unknown."""
    active = [b for b in data.get("blocks", []) if b.get("isActive")]
    if not active:
        return ()
    b = active[0]
    remaining = float((b.get("projection") or {}).get("remainingMinutes", 0))
    resets = datetime.now(timezone.utc) + timedelta(minutes=remaining)
    return (Window("session", None, float(b.get("percentUsed", 0.0)) / 100.0, resets, SESSION),)


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
        for token in candidates:
            try:
                data = _http_get_json(USAGE_URL, {"Authorization": f"Bearer {token}",
                                                  "User-Agent": USER_AGENT})
                return ProviderUsage(self.name, "oauth", now(), parse_anthropic(data))
            except (UsageFetchError, KeyError, ValueError, TypeError):
                continue  # next token, then ccusage; the cache spaces passes ≥180s apart
        cc = _ccusage_json()
        if cc is not None:
            windows = _parse_ccusage(cc)
            if windows:
                return ProviderUsage(self.name, "ccusage", now(), windows)
        return unavailable(self.name, now())


ADAPTERS: dict[str, UsageAdapter] = {"anthropic": AnthropicUsage()}


# ---- cache + fan-out -------------------------------------------------------

def cache_path(state_dir: str | Path, provider: str) -> Path:
    return Path(state_dir) / "usage" / f"{provider}.json"


def fetch_provider(name: str, state_dir: str | Path, *,
                   now: Callable[[], float] = time.time,
                   adapters: dict[str, UsageAdapter] = ADAPTERS) -> ProviderUsage:
    """Cached ≥ MIN_POLL_SECONDS per provider; only readable results are
    cached, so an outage never masks a recovery for 180s."""
    cp = cache_path(state_dir, name)
    if cp.exists():
        try:
            cached = json.loads(cp.read_text())
            if now() - cached["fetched_at"] < MIN_POLL_SECONDS:
                return usage_from_json(cached["usage"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    adapter = adapters.get(name)
    if adapter is None:
        return unavailable(name, now())
    u = adapter.fetch(Path(state_dir), now=now)
    if u.source != "unavailable":
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"fetched_at": now(), "usage": usage_to_json(u)}))
    return u


def fetch_all(cfg: Config, *, now: Callable[[], float] = time.time,
              adapters: dict[str, UsageAdapter] = ADAPTERS) -> dict[str, ProviderUsage]:
    return {p: fetch_provider(p, cfg.state_dir, now=now, adapters=adapters)
            for p in sorted(referenced_providers(cfg))}
