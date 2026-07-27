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
TMUX_TIMEOUT = 10
REAP_TIMEOUT = 10.0


class AttachRegistry:
    """Refcounts terminal viewers per issue.

    Grouped tmux sessions make concurrent viewers the designed case, and an
    SPA reload routinely overlaps the new socket with the old one. The
    attached-<N> marker is NOT advisory — the dispatcher declines to drive a
    task while it exists — so it may only be cleared when the LAST viewer
    goes. One uvicorn worker, one event loop: a plain dict is sufficient.
    """

    def __init__(self, sources):
        self._sources = sources
        self._counts: dict[int, int] = {}

    def attach(self, issue: int) -> None:
        count = self._counts.get(issue, 0) + 1
        self._counts[issue] = count
        if count == 1:
            self._sources.mark_attached(issue)

    def detach(self, issue: int) -> None:
        count = self._counts.get(issue, 0) - 1
        if count > 0:
            self._counts[issue] = count
            return
        self._counts.pop(issue, None)
        self._sources.clear_attached(issue)

    def viewers(self, issue: int) -> int:
        return self._counts.get(issue, 0)


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))


def _kill_session(view: str) -> None:
    # timeout= is what actually bounds the subprocess: asyncio.wait_for only
    # bounds the await, leaving a hung tmux parked on a default-executor
    # thread (~6 of them on a 2-vCPU box) forever.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        subprocess.run(["tmux", "kill-session", "-t", view],
                       capture_output=True, timeout=TMUX_TIMEOUT)


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


async def run_terminal(ws: WebSocket, issue: int, sources,
                       viewers: AttachRegistry, actor: str = "") -> None:
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
            # systemd services have no TERM; tmux refuses to start without
            # one ("open terminal failed: terminal does not support clear").
            os.execvpe("tmux", ["tmux", "-u", "new-session",
                                "-t", f"task-{issue}", "-s", view],
                       {**os.environ, "TERM": "xterm-256color"})
        finally:
            os._exit(127)

    viewers.attach(issue)
    # Guarded: this runs before the try/finally, so a raising event log here
    # would leak both the marker and the child process.
    with contextlib.suppress(Exception):
        sources.append_event("terminal-attach", issue=issue, actor=actor,
                             detail=view)
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
                except (ValueError, KeyError, AttributeError, TypeError,
                        OSError):
                    # Malformed frame ('"3"' -> AttributeError from .get,
                    # bad dimensions -> TypeError/OSError from ioctl):
                    # ignore rather than tear down a live session.
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        # Release this viewer FIRST so the dispatcher slot is never wedged by
        # a crash anywhere in the subsequent cleanup steps.  The marker itself
        # only clears when the last viewer leaves.
        viewers.detach(issue)
        # Log the detach before any await: a disconnect can cancel this
        # coroutine, and a cancelled await would skip everything after it.
        with contextlib.suppress(Exception):
            sources.append_event("terminal-detach", issue=issue, actor=actor,
                                 detail=view)
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
        # Guarded: this runs during loop teardown, and an unguarded raise here
        # would mask any in-flight exception and skip the remaining cleanup.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                loop.run_in_executor(None, _reap, pid),
                timeout=REAP_TIMEOUT)
