"""SSE: change detection per category, heartbeat, auth — direct ASGI probe.

starlette's TestClient buffers the complete response before returning headers,
so an unbounded SSE generator deadlocks it. Tests drive the ASGI app directly
via a background asyncio task: send() collects body chunks into a Queue; a
disconnect flag lets receive() return http.disconnect *without suspending*,
which slips past the anyio CancelScope.cancel() trick inside
Request.is_disconnected() and lets the generator detect the disconnect and
return cleanly.
"""
import asyncio
import json

from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, make_config
from web.app import create_app


FP1 = json.dumps({"board": "a", "queue": "a", "budget": "a",
                  "failures": "a", "history": "0"})
FP2 = json.dumps({"board": "B", "queue": "a", "budget": "a",
                  "failures": "a", "history": "9"})


def _sse_scope(authenticated=True):
    """Minimal ASGI scope for GET /api/events."""
    hdrs = [(b"tailscale-user-login", b"jesdi@github")] if authenticated else []
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": hdrs,
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
    }


async def _drive_sse(app, scope, sse_interval=0.01):
    """
    Run the ASGI SSE app as a background asyncio task. Returns
    (resp_headers, body_queue, disconnect_fn, wait_fn).

    disconnect_fn(): sets a flag so the next receive() call returns
        {"type": "http.disconnect"} *without suspending*.  This bypasses the
        anyio CancelScope.cancel() in Request.is_disconnected(), which only
        suppresses messages that require a checkpoint to arrive.

    wait_fn(timeout): awaits the task; raises asyncio.TimeoutError (and
        cancels the task) if the generator is still running after `timeout` s.
    """
    body_q: asyncio.Queue = asyncio.Queue()
    resp_headers: dict = {}
    disconnect_now = False
    first_receive = True

    async def receive():
        nonlocal disconnect_now, first_receive
        # First call: deliver the (empty) GET request body so Starlette's
        # request machinery doesn't block waiting for it.
        if first_receive:
            first_receive = False
            return {"type": "http.request", "body": b"", "more_body": False}
        # Non-blocking path: return disconnect immediately so anyio's
        # CancelScope.cancel() in is_disconnected() does not fire first.
        if disconnect_now:
            return {"type": "http.disconnect"}
        # Blocking path: yield to the event loop until flagged.
        while not disconnect_now:
            await asyncio.sleep(sse_interval * 0.1)
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            for k, v in msg.get("headers", []):
                resp_headers[k] = v
        elif msg["type"] == "http.response.body":
            text = msg.get("body", b"").decode()
            for line in text.split("\n"):
                if line:
                    await body_q.put(line)

    task = asyncio.create_task(app(scope, receive, send))

    def disconnect():
        nonlocal disconnect_now
        disconnect_now = True

    async def wait_done(timeout=2.0):
        await asyncio.wait_for(task, timeout=timeout)

    return resp_headers, body_q, disconnect, wait_done


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sse_requires_auth(tmp_path):
    """No Tailscale-User-Login header → 401 before the generator runs."""
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path), fake))
    assert client.get("/api/events").status_code == 401


def test_sse_emits_only_changed_keys(tmp_path):
    """Generator emits only changed category keys; mandatory headers are present."""
    fake = FakeSources()
    fake.fingerprint = FP1
    app = create_app(make_config(tmp_path), fake, sse_interval=0.01)

    async def probe():
        hdrs, q, disconnect, wait = await _drive_sse(app, _sse_scope())
        try:
            # First SSE line is the connected comment.
            first = await asyncio.wait_for(q.get(), timeout=1.0)
            assert first.startswith(":"), f"expected SSE comment, got {first!r}"

            # Mandatory response headers per the plan.
            assert hdrs.get(b"content-type", b"").startswith(b"text/event-stream"), \
                "missing text/event-stream content-type"
            assert hdrs.get(b"cache-control") == b"no-cache", \
                "missing Cache-Control: no-cache"
            assert hdrs.get(b"x-accel-buffering") == b"no", \
                "missing X-Accel-Buffering: no"

            # Mutate the fingerprint mid-stream; generator polls every 0.01 s.
            fake.fingerprint = FP2
            found = False
            for _ in range(200):
                try:
                    line = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    break
                if line.startswith("data:"):
                    payload = json.loads(line.removeprefix("data:").strip())
                    assert sorted(payload["changed"]) == ["board", "history"]
                    found = True
                    break
        finally:
            disconnect()
            await wait()
        assert found, "data: line never arrived within 200 iterations"

    asyncio.run(probe())


def test_sse_heartbeat_when_nothing_changes(tmp_path):
    """A ': heartbeat' comment fires after heartbeat_seconds of quiet polling."""
    fake = FakeSources()
    fake.fingerprint = FP1  # never changes — every poll is quiet
    app = create_app(make_config(tmp_path), fake,
                     sse_interval=0.01, heartbeat_seconds=0.05)

    async def probe():
        _, q, disconnect, wait = await _drive_sse(app, _sse_scope())
        try:
            first = await asyncio.wait_for(q.get(), timeout=1.0)
            assert first.startswith(":")
            found_heartbeat = False
            for _ in range(200):
                try:
                    line = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    break
                if line == ": heartbeat":
                    found_heartbeat = True
                    break
        finally:
            disconnect()
            await wait()
        assert found_heartbeat, (
            ": heartbeat line never arrived within 200 iterations — "
            "heartbeat logic is broken"
        )

    asyncio.run(probe())


def test_sse_terminates_on_disconnect(tmp_path):
    """Generator returns and the ASGI task completes when disconnect fires."""
    fake = FakeSources()
    fake.fingerprint = FP1
    app = create_app(make_config(tmp_path), fake, sse_interval=0.01)

    async def probe():
        _, q, disconnect, wait = await _drive_sse(app, _sse_scope())
        # Wait for the connected comment, then immediately disconnect.
        await asyncio.wait_for(q.get(), timeout=1.0)
        disconnect()
        # Task must complete within 1 s; TimeoutError = orphaned generator.
        try:
            await wait(timeout=1.0)
        except asyncio.TimeoutError:
            assert False, (
                "ASGI task did not terminate after disconnect — "
                "is_disconnected() path is broken or generator is orphaned"
            )

    asyncio.run(probe())
