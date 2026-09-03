"""Run the web console: uvicorn on 127.0.0.1:8481, loopback only.
tailscale serve is the access boundary; loopback is bypass prevention."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def ensure_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r}: the web console binds loopback only; "
            "tailscale serve is the access boundary (web-console spec, "
            "Network and auth)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m web")
    parser.add_argument("--config", default=os.environ.get(
        "AGENT_OPS_CONFIG",
        str(Path.home() / "agent-ops-state" / "targets.yaml")))
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    ensure_loopback(args.host)

    import uvicorn

    from dispatcher.config import load_config
    from dispatcher.github import GitHubClient
    from dispatcher.sessions import Sessions
    from web.app import create_app
    from web.sources import Sources

    cfg = load_config(args.config)
    sessions = Sessions(memory=cfg.session_memory, cpus=cfg.session_cpus, state_dir=cfg.state_dir)
    sources = Sources(cfg, sessions, GitHubClient())
    # The console always has live streams open (SSE).
    # Unbounded graceful shutdown waits on them forever, so SIGTERM would
    # burn systemd's full stop timeout and end in SIGKILL — a ~90 s 502
    # window on every deploy.  Bound the drain instead.
    uvicorn.run(create_app(cfg, sources), host=args.host,
                port=int(os.environ.get("AGENT_OPS_WEB_PORT", "8481")),
                log_level="warning", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
