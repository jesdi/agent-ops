"""WebSocket <-> PTY bridge. Each viewer gets a GROUPED tmux session
(`new-session -t task-<N> -s view-<token>`): independent window sizing,
and closing the browser kills only the view session, never the task's."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios

from starlette.websockets import WebSocket, WebSocketDisconnect

READ_CHUNK = 65536


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))


async def run_terminal(ws: WebSocket, issue: int, sources) -> None:
    await ws.accept()
    if not sources.session_alive(issue):
        await ws.send_text(json.dumps(
            {"type": "dead", "tail": sources.pane_tail(issue)}))
        await ws.close()
        return

    view = f"view-{secrets.token_hex(4)}"
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvp("tmux", ["tmux", "-u", "new-session",
                           "-t", f"task-{issue}", "-s", view])
    sources.mark_attached(issue)
    loop = asyncio.get_running_loop()

    async def pump_pty() -> None:
        while True:
            try:
                data = await loop.run_in_executor(
                    None, os.read, fd, READ_CHUNK)
            except OSError:
                break
            if not data:
                break
            await ws.send_bytes(data)

    pump = asyncio.create_task(pump_pty())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                os.write(fd, msg["bytes"])
            elif msg.get("text"):
                d = json.loads(msg["text"])
                if d.get("type") == "resize":
                    _resize(fd, int(d["cols"]), int(d["rows"]))
    except WebSocketDisconnect:
        pass
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        sources.clear_attached(issue)
        subprocess.run(["tmux", "kill-session", "-t", view],
                       capture_output=True, timeout=10)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError, ChildProcessError):
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, os.WNOHANG)
