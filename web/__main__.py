"""Entry point: uvicorn on loopback only (completed in a later task)."""
from __future__ import annotations

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def ensure_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r}: the web console binds loopback only; "
            "tailscale serve is the access boundary (web-console spec, "
            "Network and auth)")
