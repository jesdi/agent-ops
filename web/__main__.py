"""Run the web console: uvicorn on 127.0.0.1:8481, loopback only.
tailscale serve is the access boundary; loopback is bypass prevention."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
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

    from dispatcher.config import load_config
    from dispatcher.github import GitHubClient
    from dispatcher.sessions import Sessions
    from web.app import create_app
    from web.sources import Sources

    cfg = load_config(args.config)
    sessions = Sessions(memory=cfg.session_memory, cpus=cfg.session_cpus, state_dir=cfg.state_dir)
    sources = Sources(cfg.state_dir, sessions, GitHubClient())
    # The console always has live streams open (SSE).
    # Unbounded graceful shutdown waits on them forever, so SIGTERM would
    # burn systemd's full stop timeout and end in SIGKILL — a ~90 s 502
    # window on every deploy.  Bound the drain instead.
    app = create_app(cfg, sources)
    serve(app, cfg, args.host, int(os.environ.get("AGENT_OPS_WEB_PORT", "8481")))


def _bind_usage_socket(path: str, group: str) -> socket.socket:
    """The usage socket, mode 0660 and owned by `group` (default: ours)."""
    sock_path = Path(path)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    sock_path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    old = os.umask(0o117)
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    if group:
        shutil.chown(path, group=group)
    return sock


def serve(app, cfg, host: str, port: int) -> None:
    import uvicorn

    opts = dict(log_level="warning", timeout_graceful_shutdown=5)
    if not cfg.usage_socket:
        uvicorn.run(app, host=host, port=port, **opts)
        return
    sock = _bind_usage_socket(cfg.usage_socket, cfg.usage_socket_group)

    async def both():
        tcp = uvicorn.Server(uvicorn.Config(app, host=host, port=port, **opts))
        uds = uvicorn.Server(uvicorn.Config(app.state.usage_app, **opts))
        # Only the TCP server owns signals; the socket one stops with it.
        uds.install_signal_handlers = lambda: None
        task = asyncio.create_task(uds.serve(sockets=[sock]))
        try:
            await tcp.serve()
        finally:
            uds.should_exit = True
            await task

    asyncio.run(both())


if __name__ == "__main__":
    main()
