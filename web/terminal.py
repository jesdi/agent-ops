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
import time

from starlette.websockets import WebSocket, WebSocketDisconnect

READ_CHUNK = 65536


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))


def _kill_session(view: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", view],
                   capture_output=True)


def _reap(pid: int) -> None:
    """Reap the child process, escalating to SIGKILL after a short deadline.

    Run in an executor so the retry sleep does not block the event loop.
    After the master PTY fd is closed the child exits almost immediately
    via EIO; the deadline loop is a safety net only.
    """
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return  # already reaped or no such process
        if wpid != 0:
            return  # exited
        time.sleep(0.02)
    # Deadline exceeded — escalate to SIGKILL.
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        os.waitpid(pid, 0)  # blocking — child cannot ignore SIGKILL


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
        # exec immediately; on any failure _exit so the forked child never
        # continues past this point (it must not touch shared async state).
        try:
            os.execvp("tmux", ["tmux", "-u", "new-session",
                               "-t", f"task-{issue}", "-s", view])
        finally:
            os._exit(127)

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
                try:
                    d = json.loads(msg["text"])
                    if d.get("type") == "resize":
                        _resize(fd, int(d["cols"]), int(d["rows"]))
                except (ValueError, KeyError):
                    pass  # malformed frame — ignore rather than tear down session
    except WebSocketDisconnect:
        pass
    finally:
        # Clear marker FIRST so the dispatcher slot is never wedged by a crash
        # anywhere in the subsequent cleanup steps.
        sources.clear_attached(issue)
        pump.cancel()
        # Kill the child first: closing the slave PTY causes os.read on the
        # master to return EIO, which lets the executor thread exit promptly.
        # Closing fd before the thread exits is unsafe (fd recycling hazard).
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        # Wait for the pump task.  Use BaseException so a non-cancellation
        # exception from pump_pty (e.g. RuntimeError from ws.send_bytes after
        # close) does not skip the remaining cleanup steps.
        with contextlib.suppress(BaseException):
            await pump
        # Kill the grouped view session off the event loop to avoid stalling.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                loop.run_in_executor(None, _kill_session, view),
                timeout=10.0)
        # Close the master fd only after the reader thread has exited.
        with contextlib.suppress(OSError):
            os.close(fd)
        # Reap the child in an executor so the retry loop does not block the
        # event loop.  By now the slave PTY is closed, so the child exits fast.
        await loop.run_in_executor(None, _reap, pid)
