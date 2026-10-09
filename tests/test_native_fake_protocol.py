"""Standalone public 0.156.1 list contracts over the historical helpers' Unix WS.

No dispatcher imports, production policy, provider API, or intake runtime files.
T3's public thread state is seeded at process startup because its normal turns
contain no items. T4's populated history is created through native turn RPCs.
Run with: .venv/bin/python tests/test_native_fake_protocol.py -v
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from websockets.asyncio.client import unix_connect


CHECKOUT = Path(__file__).resolve().parents[1]
T3_BOOTSTRAP = """
import asyncio, importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('native_fixture', sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
for row in json.loads(Path(os.environ['NATIVE_SEED']).read_text()):
    fixture.thread(row['id']).update(row)
sys.argv = [sys.argv[1], 'app-server', '--listen', sys.argv[2]]
asyncio.run(fixture.backend())
"""


class NativeContract:
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="native-protocol-", dir="/tmp")
        self.root = Path(self.directory.name)
        self.path = self.root / "rpc.sock"
        self.sequence = 0
        environment = dict(os.environ)
        for name, directory in {"HOME": "home", "CODEX_HOME": "codex-home",
                                "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
                                "XDG_CACHE_HOME": "cache"}.items():
            owned = self.root / directory
            owned.mkdir()
            environment[name] = str(owned)
        environment["HISTFILE"] = str(self.root / "history")
        helper = CHECKOUT / "tests" / self.helper
        if self.helper == "t3_fake_codex.py":
            self.identity = "root"
            seed = [
                {"id": "root", "turns": [
                    {"id": "turn-a", "status": "completed", "items": [
                        {"type": "agentMessage", "id": "message-a", "text": "Public result"}]},
                    {"id": "turn-b", "status": "completed", "items": []}]},
                self.spawn("child", "root", 1),
                self.spawn("grandchild", "child", 2),
                self.spawn("foreign-child", "foreign-root", 1),
                {"id": "foreign-root"},
                {"id": "review", "parentThreadId": "root", "source": {"subAgent": "review"}},
            ]
            seed_path = self.root / "seed.json"
            seed_path.write_text(json.dumps(seed))
            (self.root / "mode").write_text("normal")
            environment.update(T3_PROVIDER_FIXTURE=str(self.root), NATIVE_SEED=str(seed_path))
            command = [sys.executable, "-c", T3_BOOTSTRAP, str(helper), "unix://" + str(self.path)]
        else:
            self.identity = "root"
            (self.root / "control.json").write_text("{}")
            environment.update(T4_EXTERNAL=str(self.root))
            command = [sys.executable, str(helper), "app-server", "--listen", "unix://" + str(self.path)]
        self.output = (self.root / "process.log").open("w+")
        self.process = subprocess.Popen(command, cwd=CHECKOUT, env=environment,
                                        stdout=self.output, stderr=subprocess.STDOUT)
        self.addAsyncCleanup(self.cleanup)
        for _ in range(200):
            if self.process.poll() is not None:
                self.output.seek(0)
                self.fail("fixture exited during setup: " + self.output.read())
            if self.path.exists():
                break
            await asyncio.sleep(.01)
        self.socket = await unix_connect(path=str(self.path))
        await self.rpc("initialize", {"clientInfo": {"name": "native-contract", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
        if self.helper == "t4_codex_external.py":
            await self.rpc("thread/start", {})
            await self.rpc("turn/start", {"threadId": self.identity,
                "clientUserMessageId": "public-message", "input": [{"type": "text", "text": "Public input"}]})
        self.turns = (await self.rpc("thread/read", {"threadId": self.identity, "includeTurns": True}))["thread"]["turns"]

    @staticmethod
    def spawn(identity, parent, depth):
        return {"id": identity, "parentThreadId": parent, "source": {"subAgent": {
            "thread_spawn": {"parent_thread_id": parent, "depth": depth}}}}

    async def cleanup(self):
        if hasattr(self, "socket"):
            await self.socket.close()
        if self.process.poll() is None:
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 5)
        self.assertIsNotNone(self.process.poll(), "owned helper must exit")
        self.output.close()
        directory = self.root
        self.directory.cleanup()
        self.assertFalse(directory.exists(), "owned socket/home directory must be removed")

    async def rpc(self, method, params):
        self.sequence += 1
        ident = self.sequence
        await self.socket.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            packet = json.loads(await asyncio.wait_for(self.socket.recv(), 3))
            if packet.get("id") == ident:
                self.assertNotIn("error", packet)
                return packet["result"]

    def page(self, result):
        self.assertIn("data", result, "native list response requires data")
        self.assertIsInstance(result["data"], list)
        self.assertIn("nextCursor", result)
        self.assertIsNone(result["nextCursor"])
        return result["data"]

    async def test_items_are_turn_id_item_entries(self):
        rows = self.page(await self.rpc("thread/items/list", {"threadId": self.identity}))
        expected = [{"turnId": turn["id"], "item": item}
                    for turn in self.turns for item in turn["items"]]
        self.assertTrue(expected, "fixture must contain real items")
        self.assertEqual(rows, expected)

    async def test_items_exact_turn_filter(self):
        turn = self.turns[0]
        rows = self.page(await self.rpc("thread/items/list", {
            "threadId": self.identity, "turnId": turn["id"]}))
        self.assertEqual(rows, [{"turnId": turn["id"], "item": item} for item in turn["items"]])

    async def test_foreign_turn_has_no_item_rows(self):
        rows = self.page(await self.rpc("thread/items/list", {
            "threadId": self.identity, "turnId": "foreign-turn"}))
        self.assertEqual(rows, [])

    async def test_turn_list_has_turn_rows(self):
        rows = self.page(await self.rpc("thread/turns/list", {
            "threadId": self.identity, "itemsView": "full"}))
        self.assertCountEqual(rows, self.turns)
        for row in rows:
            self.assertIsInstance(row["id"], str)
            self.assertIsInstance(row["items"], list)
            self.assertIn(row["status"], ("inProgress", "completed", "failed", "interrupted"))

    async def test_thread_list_contains_metadata_only(self):
        rows = self.page(await self.rpc("thread/list", {}))
        root = next(row for row in rows if row["id"] == self.identity)
        required = {"cliVersion", "createdAt", "cwd", "ephemeral", "id", "modelProvider",
                    "preview", "projectId", "sessionId", "source", "status", "turns", "updatedAt"}
        self.assertTrue(required <= root.keys(), "Thread metadata must satisfy native required fields")
        self.assertEqual(root["turns"], [])
        # Metadata-only listing must never erase the helper's stored history.
        history = (await self.rpc("thread/read", {"threadId": self.identity, "includeTurns": True}))["thread"]["turns"]
        self.assertEqual(history, self.turns)

    async def test_spawned_descendants_are_scoped_at_every_depth(self):
        rows = self.page(await self.rpc("thread/list", {
            "ancestorThreadId": self.identity, "sourceKinds": ["cli", "subAgentThreadSpawn"], "modelProviders": []}))
        expected = {"child", "grandchild"} if self.helper == "t3_fake_codex.py" else set()
        self.assertEqual({row["id"] for row in rows}, expected)
        for row in rows:
            self.assertEqual(row["turns"], [])
            self.assertEqual(row["parentThreadId"], row["source"]["subAgent"]["thread_spawn"]["parent_thread_id"])

    async def test_direct_parent_excludes_grandchildren(self):
        rows = self.page(await self.rpc("thread/list", {
            "parentThreadId": self.identity, "sourceKinds": ["cli", "subAgentThreadSpawn"]}))
        expected = {"child"} if self.helper == "t3_fake_codex.py" else set()
        self.assertEqual({row["id"] for row in rows}, expected)

    async def test_spawn_source_filter_without_ancestry(self):
        rows = self.page(await self.rpc("thread/list", {"sourceKinds": ["subAgentThreadSpawn"]}))
        expected = {"child", "grandchild", "foreign-child"} if self.helper == "t3_fake_codex.py" else set()
        self.assertEqual({row["id"] for row in rows}, expected)

    async def test_default_and_empty_sources_select_interactive_threads(self):
        for params in ({}, {"sourceKinds": []}):
            rows = self.page(await self.rpc("thread/list", params))
            self.assertTrue(rows)
            self.assertTrue(all(row["source"] in ("cli", "vscode", "appServer") for row in rows))


class T3NativeProtocol(NativeContract, unittest.IsolatedAsyncioTestCase):
    helper = "t3_fake_codex.py"


class T4NativeProtocol(NativeContract, unittest.IsolatedAsyncioTestCase):
    helper = "t4_codex_external.py"


if __name__ == "__main__":
    unittest.main()
