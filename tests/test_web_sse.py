"""SSE: change detection per category, heartbeat, auth.

Design note: starlette's TestClient (and httpx's ASGITransport) buffer the
entire response body before returning it to the caller, so truly infinite SSE
streams cannot be tested with `client.stream()`. The tests therefore use
`sse_max_events` to cap the generator at a small iteration count, pre-sequence
the FakeSources fingerprints to drive the expected events deterministically,
and then read the buffered lines with a bounded `for _ in range(200)` loop.
The bound makes regressions fail loudly rather than hang.
"""
import json

from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


FP1 = json.dumps({"board": "a", "queue": "a", "budget": "a",
                  "failures": "a", "history": "0"})
FP2 = json.dumps({"board": "B", "queue": "a", "budget": "a",
                  "failures": "a", "history": "9"})


def test_sse_requires_auth(tmp_path):
    fake = FakeSources()
    client = TestClient(create_app(make_config(tmp_path), fake,
                                   sse_interval=0.01, sse_max_events=1))
    assert client.get("/api/events").status_code == 401


def test_sse_emits_only_changed_keys(tmp_path):
    # Pre-sequence: first fingerprint read (before loop) = FP1; all loop reads
    # = FP2, so the very first poll detects ["board", "history"] as changed.
    fake = FakeSources()
    fake._fp_seq = [FP1] + [FP2] * 10
    client = TestClient(
        create_app(make_config(tmp_path), fake,
                   sse_interval=0.001,
                   heartbeat_seconds=100.0,
                   sse_max_events=5))

    with client.stream("GET", "/api/events", headers=HEADERS) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = r.iter_lines()
        assert next(lines).startswith(":")  # connected comment
        found = False
        for _ in range(200):
            try:
                line = next(lines)
            except StopIteration:
                break
            if line.startswith("data:"):
                payload = json.loads(line.removeprefix("data:").strip())
                assert sorted(payload["changed"]) == ["board", "history"]
                found = True
                break
    assert found, "data: line never arrived within 200 iterations"


def test_sse_heartbeat_when_nothing_changes(tmp_path):
    # heartbeat_seconds=0.05 with sse_interval=0.01 → heartbeat fires after
    # 5 quiet polls; sse_max_events=20 → at least 4 heartbeats emitted.
    fake = FakeSources()
    fake.fingerprint = FP1  # unchanged throughout; every poll is quiet
    client = TestClient(
        create_app(make_config(tmp_path), fake,
                   sse_interval=0.01,
                   heartbeat_seconds=0.05,
                   sse_max_events=20))

    with client.stream("GET", "/api/events", headers=HEADERS) as r:
        lines = r.iter_lines()
        first = next(lines)
        assert first.startswith(":")  # connected comment
        found_heartbeat = False
        for _ in range(200):
            try:
                line = next(lines)
            except StopIteration:
                break
            if line == ": heartbeat":
                found_heartbeat = True
                break
    assert found_heartbeat, (
        ": heartbeat line never arrived within 200 iterations — "
        "heartbeat logic is broken"
    )
