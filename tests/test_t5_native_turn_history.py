"""Own external-wire conformance for native 0.156.1 paged turn history.

Literal requests/turns follow ThreadTurnsListParams/Response and ThreadReadParams.
No product process, provider credentials or external schema file is required.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from t5_executable_support import _stop_owned_group
from t5_provider.ws_wire import connect, control


METHOD = "thread/turns/list"
PROVIDER = Path(__file__).resolve().parent / "t5_provider" / "native_fake.py"
ROOT = "native-turn-root"


class WirePeer:
    def __init__(self, wire, action_path):
        self.wire = wire
        self.action_path = action_path
        self.sequence = 0

    async def call(self, method, **params):
        self.sequence += 1
        identifier = "turn-conformance-" + str(self.sequence)
        await self.wire.send({"id": identifier, "method": method, "params": params})
        while True:
            response = await asyncio.wait_for(self.wire.receive(), 5)
            if response.get("id") == identifier:
                return response

    async def result(self, method=METHOD, **params):
        response = await self.call(method, **params)
        assert "result" in response, f"declared native {method} capability missing or rejected: {response}"
        return response["result"]

    async def action(self, action, **fields):
        return await control(str(self.action_path), {"action": action, **fields})


@asynccontextmanager
async def external_wire():
    with tempfile.TemporaryDirectory(prefix="t5turns-", dir="/tmp") as temporary:
        directory = Path(temporary)
        socket = directory / "native.sock"
        actions = directory / "actions.sock"
        env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
               "FAKE_CODEX_CONTROL": str(actions), "FAKE_CODEX_CWD": str(directory),
               "FAKE_CODEX_JOURNAL": str(directory / "native.jsonl"),
               "FAKE_CODEX_PAGE_SIZE": "2", "PYTHONPYCACHEPREFIX": str(directory / "bytecode")}
        for field in ["HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"]:
            env[field] = str(directory / field.lower())
            Path(env[field]).mkdir()
        env["HISTFILE"] = str(directory / "history")
        with (directory / "backend.log").open("w+") as log:
            process = subprocess.Popen([sys.executable, "-B", str(PROVIDER), "app-server", "--listen", "unix://" + str(socket)],
                                       cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            wire = None
            try:
                for _ in range(300):
                    if socket.exists() and actions.exists():
                        break
                    assert process.poll() is None, "owned fake exited before native wire readiness"
                    await asyncio.sleep(0.01)
                assert socket.exists() and actions.exists(), "owned native wire readiness barrier"
                wire = await connect(str(socket))
                peer = WirePeer(wire, actions)
                await peer.result("initialize", clientInfo={"name": "turn-conformance", "version": "0.156.1"},
                                  capabilities={"experimentalApi": True})
                await wire.send({"method": "initialized", "params": {}})
                yield peer
            finally:
                if wire is not None:
                    await wire.close()
                if actions.exists():
                    await control(str(actions), {"action": "shutdown"})
                assert _stop_owned_group(process), "owned native fake group must disappear before strict cleanup"
                assert not socket.exists() and not actions.exists(), "native sockets must be removed by owned backend shutdown"


async def seed(peer):
    await peer.action("create_thread", thread_id=ROOT)
    await peer.action("create_thread", thread_id="foreign-thread")
    expected = []
    for number, status in enumerate(["completed", "completed", "failed", "interrupted", "completed", "inProgress"], 1):
        turn_id = "native-turn-" + str(number)
        items = [{"type": "agentMessage", "id": "native-message", "text": "available native message"}] if number == 2 else []
        if number == 6:
            items = [{"type": "userMessage", "id": "native-user", "clientId": "generic-client",
                      "content": [{"type": "text", "text": "generic input"}]}]
        turn = await peer.action("start_turn", thread_id=ROOT, turn_id=turn_id, items=items)
        if number == 5:
            await peer.action("start_command", thread_id=ROOT, turn_id=turn_id, item_id="native-command")
            await peer.action("end_command", thread_id=ROOT, item_id="native-command", status="failed",
                              exit_code=7, output="AVAILABLE_NATIVE_SUFFIX", duration_ms=12)
        if status != "inProgress":
            error = {"message": "GENERIC_NATIVE_FAILURE", "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}},
                     "additionalDetails": "available native error detail"} if status == "failed" else None
            turn = await peer.action("end_turn", thread_id=ROOT, turn_id=turn_id, status=status, error=error)
        expected.append(turn)
    await peer.action("start_turn", thread_id="foreign-thread", turn_id="foreign-only")
    await peer.action("end_turn", thread_id="foreign-thread", turn_id="foreign-only")
    return expected


async def pages(peer, **params):
    found = []
    seen = set()
    cursor = None
    for _ in range(20):
        page = await peer.result(**params, cursor=cursor)
        assert set(page) == {"data", "nextCursor", "backwardsCursor"}
        assert isinstance(page["data"], list)
        for turn in page["data"]:
            assert {"id", "items", "status"} <= turn.keys()
            assert turn["status"] in {"inProgress", "completed", "failed", "interrupted"}
            assert turn["itemsView"] in {"notLoaded", "summary", "full"}
            assert isinstance(turn["items"], list)
        found.extend(page["data"])
        cursor = page["nextCursor"]
        if cursor is None:
            return found
        assert isinstance(cursor, str) and cursor and cursor not in seen
        seen.add(cursor)
    pytest.fail("native turn history did not reach bounded opaque-cursor exhaustion")


@pytest.mark.parametrize("direction", [None, "asc", "desc"], ids=["default-desc", "asc", "desc"])
def test_turn_history_pages_preserve_successive_and_itemless_turns_independent_of_reduced_read(direction):
    async def scenario():
        async with external_wire() as peer:
            expected = await seed(peer)
            native = (await peer.action("snapshot"))["threads"][ROOT]
            reduced = deepcopy(native)
            reduced["turns"] = [deepcopy(expected[-1])]
            reduced["turns"][0].update({"items": [], "itemsView": "summary"})
            await peer.action("fault_next", matches={"method": "thread/read", "threadId": ROOT},
                              fault={"result": {"thread": reduced}})
            read = await peer.result("thread/read", threadId=ROOT, includeTurns=True)
            assert [t["id"] for t in read["thread"]["turns"]] == ["native-turn-6"]
            await peer.action("history_view", thread_id=ROOT, items_view="notLoaded")
            found = await pages(peer, threadId=ROOT, itemsView="full", limit=1, sortDirection=direction)
            assert found == (expected if direction == "asc" else list(reversed(expected)))
            assert len(found) == 6 and "foreign-only" not in {t["id"] for t in found}
            snapshot = await peer.action("snapshot")
            assert snapshot["threads"][ROOT] == native, "turn paging must not mutate native turns/state"
            assert all(p["subscriptions"] == [] for p in snapshot["peers"])
    asyncio.run(scenario())


@pytest.mark.parametrize("view", [None, "summary", "notLoaded", "full"], ids=["default-summary", "summary", "not-loaded", "full"])
def test_turn_history_items_view_preserves_exact_status_errors_and_full_available_items(view):
    async def scenario():
        async with external_wire() as peer:
            expected = await seed(peer)
            found = await pages(peer, threadId=ROOT, itemsView=view, sortDirection="asc", limit=2)
            assert [t["id"] for t in found] == [t["id"] for t in expected]
            for actual, native in zip(found, expected):
                assert actual["status"] == native["status"] and actual["error"] == native["error"]
                assert actual["itemsView"] == (view or "summary")
                if view == "full":
                    assert actual["items"] == native["items"]
                elif view == "notLoaded":
                    assert actual["items"] == []
                else:
                    assert all(item in native["items"] for item in actual["items"])
            if view == "full":
                command = found[4]["items"][0]
                assert command["type"] == "commandExecution" and command["exitCode"] == 7
                assert command["aggregatedOutput"] == "AVAILABLE_NATIVE_SUFFIX" and command["durationMs"] == 12
    asyncio.run(scenario())


def test_turn_history_forward_cursors_isolate_scope_and_backwards_cursor_includes_updated_anchor():
    async def scenario():
        async with external_wire() as peer:
            await seed(peer)
            first = await peer.result(threadId=ROOT, itemsView="full", sortDirection="desc", limit=2)
            assert [t["id"] for t in first["data"]] == ["native-turn-6", "native-turn-5"]
            foreign = await peer.result(threadId="foreign-thread", itemsView="full")
            assert [t["id"] for t in foreign["data"]] == ["foreign-only"] and foreign["nextCursor"] is None
            await peer.action("create_thread", thread_id="empty-thread")
            empty = await peer.result(threadId="empty-thread")
            assert empty == {"data": [], "nextCursor": None, "backwardsCursor": None}
            second = await peer.result(threadId=ROOT, itemsView="full", sortDirection="desc", limit=2, cursor=first["nextCursor"])
            assert [t["id"] for t in second["data"]] == ["native-turn-4", "native-turn-3"]
            for method, params in [(METHOD, {"threadId": "foreign-thread", "sortDirection": "desc"}),
                                   (METHOD, {"threadId": ROOT, "sortDirection": "asc"}),
                                   ("thread/items/list", {"threadId": ROOT})]:
                response = await peer.call(method, **params, cursor=first["nextCursor"])
                assert response["error"]["code"] == -32602
            item_page = await peer.result("thread/items/list", threadId=ROOT, limit=1)
            response = await peer.call(METHOD, threadId=ROOT, cursor=item_page["nextCursor"])
            assert response["error"]["code"] == -32602
            update = {"type": "agentMessage", "id": "anchor-update", "text": "available late anchor update"}
            await peer.action("end_turn", thread_id=ROOT, turn_id="native-turn-4", status="interrupted", items=[update])
            backwards = await peer.result(threadId=ROOT, sortDirection="asc", itemsView="full", limit=2,
                                          cursor=second["backwardsCursor"])
            assert [t["id"] for t in backwards["data"]] == ["native-turn-4", "native-turn-5"]
            assert update in backwards["data"][0]["items"]
            invalid = await peer.call(METHOD, threadId=ROOT, sortDirection="desc", cursor=second["backwardsCursor"])
            assert invalid["error"]["code"] == -32602
            final = await peer.result(threadId=ROOT, sortDirection="desc", itemsView="full", cursor=second["nextCursor"])
            assert [t["id"] for t in final["data"]] == ["native-turn-2", "native-turn-1"] and final["nextCursor"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [{}, {"threadId": "missing"}, {"threadId": ROOT, "cursor": "foreign-opaque"},
    {"threadId": ROOT, "sortDirection": "other"}, {"threadId": ROOT, "itemsView": "other"},
    {"threadId": ROOT, "limit": -1}, {"threadId": ROOT, "limit": True}, {"threadId": ROOT, "limit": 1.5},
    {"threadId": ROOT, "limit": 4294967296}, {"threadId": ROOT, "includeItems": True},
    {"threadId": False}, {"threadId": ROOT, "cursor": 12}, {"threadId": ROOT, "sortDirection": False},
    {"threadId": ROOT, "sortDirection": ""}, {"threadId": ROOT, "itemsView": []}],
    ids=["missing-thread", "unknown-thread", "unknown-cursor", "direction", "view", "negative-limit", "boolean-limit", "fractional-limit", "uint32-overflow", "undeclared-field", "thread-type", "cursor-type", "direction-type", "empty-direction", "view-type"])
def test_native_turn_history_invalid_scope_and_declared_parameter_errors(invalid):
    async def scenario():
        async with external_wire() as peer:
            await seed(peer)
            await peer.result(threadId=ROOT)
            response = await peer.call(METHOD, **invalid)
            assert response["error"]["code"] == -32602
    asyncio.run(scenario())


def test_native_turn_history_uses_existing_fault_barrier_and_bounded_zero_limit_fixture_choice():
    async def scenario():
        async with external_wire() as peer:
            expected = await seed(peer)
            found = await pages(peer, threadId=ROOT, limit=0, itemsView="full", sortDirection="asc")
            assert found == expected  # Own server normalization; no native zero-limit behavior claim.
            await peer.action("fault_next", matches={"method": METHOD}, fault="error")
            response = await peer.call(METHOD, threadId=ROOT)
            assert response["error"]["code"] == -32603
            await peer.action("hold_next", name="turn-page", matches={"method": METHOD}, phase="after")
            pending = asyncio.create_task(peer.call(METHOD, threadId=ROOT, limit=1))
            await peer.action("wait", matches={"kind": "request_held", "name": "turn-page"})
            assert not pending.done()
            await peer.action("release", name="turn-page")
            released = await pending
            assert released["result"]["data"][0]["id"] == "native-turn-6"
    asyncio.run(scenario())
