"""Run the web console: uvicorn on 127.0.0.1:8481, loopback only.
tailscale serve is the access boundary; loopback is bypass prevention."""
from __future__ import annotations

import argparse
import importlib
import os
import threading
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def ensure_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r}: the web console binds loopback only; "
            "tailscale serve is the access boundary (web-console spec, "
            "Network and auth)")


def _project_control(state_dir: str):
    """The trusted control service (its GitHub and deployer adapters hold the control
    credential, so they live with the box, not here): AGENT_OPS_PROJECT_CONTROL names
    `module:factory`, called with the state directory. Unset: the console reads and
    writes no project changes."""
    spec = os.environ.get("AGENT_OPS_PROJECT_CONTROL")
    if not spec:
        return None
    module, _, factory = spec.partition(":")
    return getattr(importlib.import_module(module), factory)(state_dir)


def _advance_once(control) -> None:
    try:
        control.advance_controls()
    except Exception:  # noqa: BLE001 — a failing pass is retried on the next tick
        pass


def _advance_until(stop: threading.Event, control, interval: float = 5.0) -> None:
    """Drive accepted enable / pause requests through PR, merge and deployment until `stop`."""
    while not stop.wait(interval):
        _advance_once(control)


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
    # No credential service here: a scoped session's live output is withheld
    # (Sessions._redacted); parked ones show the dispatcher's redacted snapshot.
    sessions = Sessions(memory=cfg.session_memory, cpus=cfg.session_cpus, state_dir=cfg.state_dir)
    sources = Sources(cfg.state_dir, sessions, GitHubClient())
    # The console always has live streams open (SSE).
    # Unbounded graceful shutdown waits on them forever, so SIGTERM would
    # burn systemd's full stop timeout and end in SIGKILL — a ~90 s 502
    # window on every deploy.  Bound the drain instead.
    control = _project_control(cfg.state_dir)
    if control is not None:
        threading.Thread(target=_advance_until, args=(threading.Event(), control), daemon=True).start()
    uvicorn.run(create_app(cfg, sources, config_path=args.config,
                           project_control=control), host=args.host,
                port=int(os.environ.get("AGENT_OPS_WEB_PORT", "8481")),
                log_level="warning", timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
