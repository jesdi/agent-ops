"""Run the web console: uvicorn on 127.0.0.1:8481, loopback only.
tailscale serve is the access boundary; loopback is bypass prevention."""
from __future__ import annotations

import argparse
import asyncio
import os
import grp
import socket
import stat
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
    """The usage socket: directory 0750, socket 0660, both in `group` (default: ours).

    File-system permissions are the only boundary, so nothing is created or
    deleted until the group and the path have been checked."""
    sock_path = Path(path)
    gid = os.getgid()
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            raise SystemExit(f"usage_socket_group {group!r} does not exist") from None
    def leftover():
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISSOCK(st.st_mode):
            raise SystemExit(
                f"refusing to replace {path}: it is not a socket (a leftover "
                "usage socket is the only thing start-up removes)")
        return True

    leftover()
    parent = sock_path.parent
    old = os.umask(0o022)  # missing ancestors 0755, never writable by others
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(old)
    _refuse_replaceable_ancestors(parent)
    pst = os.lstat(parent)  # never chown/chmod a symlink or someone else's directory
    if not stat.S_ISDIR(pst.st_mode) or pst.st_uid != os.getuid():
        raise SystemExit(f"refusing to use {parent}: not a directory owned by this user")
    os.chown(parent, -1, gid)
    os.chmod(parent, 0o750)
    # Checked again now the directory is ours alone: before, another user
    # could have swapped the entry between the check and the unlink.
    if leftover():
        sock_path.unlink()
    sock = socket.socket(socket.AF_UNIX)
    old = os.umask(0o117)  # created 0660 from the first moment
    try:
        sock.bind(path)
    finally:
        os.umask(old)
    os.chown(path, -1, gid)
    return sock


def _crossed_entries(path: str):
    """Every directory and symlink the kernel crosses resolving `path`,
    following each hop the way it does, so no hop goes unchecked. `..` is
    kept until the symlinks before it are followed, as the kernel does."""
    todo, cur, hops = list(Path(os.getcwd(), path).parts), Path("/"), 0
    yield cur
    while todo:
        part = todo.pop(0)
        if part == "/":
            cur = Path("/")
        elif part == "..":
            cur = cur.parent
        elif part != ".":
            nxt = cur / part
            if stat.S_ISLNK(os.lstat(nxt).st_mode):
                hops += 1
                if hops > 40:
                    raise SystemExit(f"refusing to use {path}: too many symlinks")
                yield nxt
                todo[:0] = Path(os.readlink(nxt)).parts
                continue
            cur = nxt
            yield cur


def _refuse_replaceable_ancestors(parent: Path) -> None:
    """Whoever can write an ancestor of the socket directory, or owns a
    symlink on the way, can swap the directory after it is secured, so each
    entry crossed must be owned by root or us, and each directory writable by
    no one else unless sticky (like /tmp, where only an entry's owner can
    replace it)."""
    for a in _crossed_entries(str(parent.parent)):
        st = os.lstat(a)
        if st.st_uid not in (0, os.getuid()) or (
                stat.S_ISDIR(st.st_mode) and st.st_mode & 0o022
                and not st.st_mode & stat.S_ISVTX):
            raise SystemExit(
                f"refusing to use {parent}: its ancestor {a} can be "
                "replaced by another user")


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
