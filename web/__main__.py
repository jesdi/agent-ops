"""Run the web console: uvicorn on 127.0.0.1:8481, loopback only.
tailscale serve is the access boundary; loopback is bypass prevention."""
from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
from pathlib import Path

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
TMUX_TIMEOUT = 10


def sweep_stale_terminals(state_dir, *, run=subprocess.run) -> None:
    """Drop terminal state left behind by a hard kill.

    run_terminal's finally covers exceptions but not SIGKILL, and on a 4 GB
    box (ADR 0002) OOM kills are expected.  A stale attached-<N> wedges that
    task permanently — the dispatcher returns from _drive_task every pass
    while the marker exists, with no timeout and no janitor.  Grouped view-*
    sessions live on the tmux server, outside this unit's cgroup, so they
    survive the kill too.  A freshly started web process has zero terminals
    by definition, so both are unconditionally safe to clear here.
    """
    from dispatcher import state

    for path in sorted(Path(state_dir).glob("attached-*")):
        try:
            issue = int(path.name.split("-", 1)[1])
        except ValueError:
            continue
        state.clear_attached(state_dir, issue)

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        listed = run(["tmux", "list-sessions", "-F", "#{session_name}"],
                     capture_output=True, text=True, timeout=TMUX_TIMEOUT)
        for name in (listed.stdout or "").split():
            if name.startswith("view-"):
                run(["tmux", "kill-session", "-t", name],
                    capture_output=True, timeout=TMUX_TIMEOUT)


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
    sessions = Sessions(memory=cfg.session_memory, cpus=cfg.session_cpus)
    sources = Sources(cfg, sessions, GitHubClient())
    sweep_stale_terminals(cfg.state_dir)
    uvicorn.run(create_app(cfg, sources), host=args.host,
                port=int(os.environ.get("AGENT_OPS_WEB_PORT", "8481")),
                log_level="warning")


if __name__ == "__main__":
    main()
