"""Spawn/no-spawn gatekeeper for every stage spawn (v1).

Research feeding this module: portfolio_eval#155."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UsageSnapshot:
    utilization: float       # 0..1 of the 5-hour session window
    minutes_to_reset: float
    source: str              # "oauth" | "ccusage" | "unavailable"


def should_spawn(
    u: UsageSnapshot,
    threshold: float,
    racing_minutes: int,
    racing_threshold: float,
) -> bool:
    if u.source == "unavailable":
        return False
    # Reset-racing bonus: window resets soon → relax the ceiling so the
    # remainder of the window isn't wasted.
    limit = racing_threshold if u.minutes_to_reset <= racing_minutes else threshold
    return u.utilization < limit


import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Mandatory per community findings: requests without this UA get 429'd.
USER_AGENT = "claude-code/2.0.0"
MIN_POLL_SECONDS = 180
HOST_CREDENTIALS = "~/.claude/.credentials.json"


class UsageFetchError(Exception):
    pass


def _http_get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise UsageFetchError(str(e)) from e


def _ccusage_json() -> dict | None:
    try:
        out = subprocess.run(
            ["ccusage", "blocks", "--json"],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _read_token(credentials_path: str | Path) -> str | None:
    p = Path(credentials_path).expanduser()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _parse_oauth(data: dict) -> UsageSnapshot:
    # Assumed shape pending portfolio_eval#155; adjust here only.
    window = data["five_hour"]
    resets_at = datetime.fromisoformat(window["resets_at"])
    mins = (resets_at - datetime.now(timezone.utc)).total_seconds() / 60
    return UsageSnapshot(
        utilization=float(window["utilization"]) / 100.0,
        minutes_to_reset=max(0.0, mins),
        source="oauth",
    )


def _parse_ccusage(data: dict) -> UsageSnapshot | None:
    active = [b for b in data.get("blocks", []) if b.get("isActive")]
    if not active:
        return None
    b = active[0]
    remaining = (b.get("projection") or {}).get("remainingMinutes", 0)
    return UsageSnapshot(
        utilization=float(b.get("percentUsed", 0.0)) / 100.0,
        minutes_to_reset=float(remaining),
        source="ccusage",
    )


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


def fetch_usage(
    state_dir: str | Path,
    credentials_path: str | Path | None = None,
    now: Callable[[], float] = time.time,
) -> UsageSnapshot:
    """OAuth endpoint (cached, ≥180s between polls) → ccusage → unavailable."""
    cache_path = Path(state_dir) / "usage-cache.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if now() - cached["fetched_at"] < MIN_POLL_SECONDS:
                return UsageSnapshot(**cached["snapshot"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Token preference: explicit env override, then the box's long-lived
    # setup-token read from 1P at fetch time (no unit carries the secret in
    # its process env — a session shell would inherit the dispatcher's),
    # then the shared OAuth store. The store stays as a live fallback: the
    # usage endpoint is unofficial and may reject the static token.
    candidates = [t for t in (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
        _token_from_op(),
        _read_token(_resolve_credentials(state_dir, credentials_path)),
    ) if t]
    for token in candidates:
        try:
            data = _http_get_json(USAGE_URL, {
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
            })
            snap = _parse_oauth(data)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({
                "fetched_at": now(),
                "snapshot": snap.__dict__,
            }))
            return snap
        except (UsageFetchError, KeyError, ValueError, TypeError):
            continue  # next token, then ccusage; no retry storm — the
            # cache spaces passes ≥180s apart either way

    cc = _ccusage_json()
    if cc is not None:
        snap = _parse_ccusage(cc)
        if snap is not None:
            return snap
    return UsageSnapshot(utilization=1.0, minutes_to_reset=0.0, source="unavailable")
