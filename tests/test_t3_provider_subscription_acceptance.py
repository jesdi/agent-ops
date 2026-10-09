"""External fixture conformance to captured Codex 0.156.1 subscriptions.

Fresh read/inventory RPCs do not subscribe future main-turn lifecycle;
exact-root resume does, without adding a model turn. This tests the fake
provider over real sockets, independently of the production supervisor.
"""
import asyncio
import json
import os
import subprocess
import sys

from websockets.asyncio.client import unix_connect

from dispatcher.runtime_http import RuntimeClient
from tests.test_bound_turns_t3_acceptance import PROVIDER, eventually, isolated, terminate  # noqa: F401


def test_provider_fixture_distinguishes_fresh_read_from_exact_root_resume_subscription(isolated):
    root, state, worktree = isolated
    fixture = root / "provider"
    fixture.mkdir()
    (fixture / "mode").write_text("compatible")
    binding = RuntimeClient(state).prepare("project-a", 370, "review", worktree=str(worktree))["binding"]
    environment = {**os.environ, "T3_PROVIDER_FIXTURE": str(fixture),
                   **{f"AGENT_OPS_{key.upper()}": str(value or "") for key, value in binding.items()}}
    socket = root / "provider.sock"
    output = (fixture / "process.log").open("w+")
    process = subprocess.Popen([sys.executable, str(PROVIDER), "app-server", "--listen", f"unix://{socket}"],
                               cwd=worktree, env=environment, stdout=output, stderr=output,
                               start_new_session=True)

    async def protocol():
        async def rpc(connection, identity, method, params=None):
            await connection.send(json.dumps(dict(id=identity, method=method, params=params or {})))
            while True:
                response = json.loads(await connection.recv())
                if response.get("id") == identity:
                    assert "error" not in response, response
                    return response["result"]

        async def notification(connection, method):
            try:
                while True:
                    message = json.loads(await asyncio.wait_for(connection.recv(), .4))
                    if message.get("method") == method:
                        return message
            except asyncio.TimeoutError:
                return None

        async with unix_connect(path=str(socket)) as creator, \
                unix_connect(path=str(socket)) as reader, unix_connect(path=str(socket)) as resumed:
            for connection in (creator, reader, resumed):
                await rpc(connection, "initialize", "initialize", {"clientInfo": {"name": "subscription-smoke", "version": "1"}})
                await connection.send(json.dumps({"method": "initialized"}))
            started = await rpc(creator, "create", "thread/start")
            identity = started["thread"]["id"]
            assert RuntimeClient(state).event(binding, {"type": "bound", "conversation_id": identity})
            active = await rpc(creator, "actual-prompt", "turn/start",
                               {"threadId": identity, "input": [{"type": "text", "text": "One actual prompt"}]})
            turn_id = active["turn"]["id"]
            await rpc(reader, "read", "thread/read", {"threadId": identity, "includeTurns": True})
            assert (await rpc(reader, "inventory", "thread/backgroundTerminals/list", {"threadId": identity}))["data"] == []
            recovered = await rpc(resumed, "resume", "thread/resume", {
                "threadId": identity, "cwd": str(worktree), "model": "gpt-6.1-sol",
                "approvalPolicy": "never", "sandbox": "danger-full-access",
                "config": {"model_reasoning_effort": "high"},
            })
            assert recovered["thread"]["id"] == identity
            assert recovered["thread"]["turns"][-1]["id"] == turn_id
            assert recovered["thread"]["turns"][-1]["status"] == "inProgress"
            with (fixture / "commands.jsonl").open("a") as commands:
                commands.write(json.dumps({"action": "stop", "threadId": identity}) + "\n")
            completion = await notification(resumed, "turn/completed")
            assert completion, "exact-root resume must subscribe subsequent native main-turn completion"
            assert completion["params"]["threadId"] == identity
            assert completion["params"]["turn"]["id"] == turn_id
            assert completion["params"]["turn"]["status"] == "completed"
            assert await notification(reader, "turn/completed") is None, (
                "initialize + read + inventory must not subscribe future main-turn lifecycle")

    try:
        eventually(lambda: socket.exists(), "standalone provider fixture must open its Unix WebSocket")
        asyncio.run(protocol())
        records = [json.loads(line) for line in (fixture / "provider.jsonl").read_text().splitlines()]
        assert len([record for record in records if record["kind"] == "rpc" and record["method"] == "turn/start"]) == 1
    finally:
        terminate(process)
        output.close()
