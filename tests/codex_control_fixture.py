"""Additional external wire scenarios for the supervisor public executable.

Reuse the locked fake provider's ordinary Codex protocol; inject only external
wire failures and server requests. No dispatcher code is imported here.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("fake_codex", Path(__file__).with_name("t3_fake_codex.py"))
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
receipt_reads = 0


class Socket:
    def __init__(self, socket):
        self.socket = socket
        self.methods = {}
        self.subscribed = False
        self.controller = False
        self.initial_input = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            packet = json.loads(await self.socket.recv())
            if "method" in packet:
                self.methods[packet.get("id")] = packet["method"]
                if packet["method"] == "initialize":
                    self.controller = packet["params"]["clientInfo"]["name"] == "agent-ops"
                if packet["method"] in ("thread/start", "thread/resume"):
                    self.subscribed = True
                if packet["method"] == "turn/start":
                    self.initial_input = packet["params"]
                return json.dumps(packet)
            fake.record("client-response", packet=packet)

    async def send(self, raw):
        global receipt_reads
        packet = json.loads(raw)
        mode = (fake.ROOT / "mode").read_text()
        if (mode.startswith("bootstrap-recover") or mode == "bootstrap-lost-ack") and self.methods.get(packet.get("id")) == "turn/start":
            turn = fake.thread(self.initial_input["threadId"])["turns"][-1]
            turn["items"] = [{"type": "userMessage", "id": "initial-input",
                              "clientId": self.initial_input.get("clientUserMessageId"),
                              "content": self.initial_input["input"]}]
            fake.record("bootstrap-ack-dropped")
            return
        if mode.startswith("bootstrap-recover") and self.initial_input and packet.get("method") == "turn/started":
            if mode in {"bootstrap-recover-completed", "bootstrap-recover-failed", "bootstrap-recover-interrupted",
                        "bootstrap-recover-failed-system-error"}:
                thread = fake.thread(self.initial_input["threadId"])
                thread["status"] = {"type": "idle"}
                thread["turns"][-1]["status"] = mode.removeprefix("bootstrap-recover-")
                if mode == "bootstrap-recover-failed-system-error":
                    thread["status"] = {"type": "systemError"}
                    thread["turns"][-1].update(status="failed", error={"message": "Provider rejected input"})
            if mode == "bootstrap-recover-later-turn":
                thread = fake.thread(self.initial_input["threadId"])
                thread["turns"][-1]["status"] = "completed"
                thread["turns"].append({"id": "later-native-turn", "status": "inProgress", "items": []})
            fake.record("bootstrap-start-dropped")
            await self.socket.close(code=1012, reason="initial reply and lifecycle lost")
            return
        if (mode.startswith("bootstrap-recover-") and self.controller and fake.NEXT_TURN
                and self.methods.get(packet.get("id")) == "thread/resume"):
            thread = packet["result"]["thread"]
            variant = mode.removeprefix("bootstrap-recover-")
            if variant == "delayed":
                receipt_reads += 1
                if receipt_reads == 1:
                    thread["turns"][-1]["items"] = []
            if variant == "foreign":
                thread["id"] = "foreign-newest"
            elif variant == "turns-shape":
                thread["turns"] = "unreadable"
            elif variant == "active-empty":
                thread["turns"] = []
            elif variant == "idle-empty":
                thread["turns"] = []
                thread["status"] = {"type": "idle"}
            elif variant == "turn-shape":
                thread["turns"][-1] = []
            elif variant in {"turn-id-type", "turn-id-empty"}:
                thread["turns"][-1]["id"] = None if variant == "turn-id-type" else ""
            elif variant == "inconsistent-status":
                thread["turns"][-1]["status"] = "completed"
            elif variant == "items-shape":
                thread["turns"][-1]["items"] = "unreadable"
            elif variant == "old-input":
                thread["turns"][-1]["items"] = [None, {"type": "agentMessage"},
                    {"type": "userMessage", "clientId": "previous-launch-input"}]
            elif variant == "invalid-status":
                thread["status"] = {"type": "idle"}
                thread["turns"][-1]["status"] = "unknown-native-status"
            elif variant == "historical-invalid-turn":
                thread["turns"][-1]["id"] = ""
                thread["turns"].append({"id": "later-native-turn", "status": "inProgress", "items": []})
            fake.record("bootstrap-recovery-response", variant=variant)
        if mode == "completion-on-resume" and packet.get("method", "").startswith("turn/") and not self.subscribed:
            return
        if self.methods.get(packet.get("id")) == "thread/backgroundTerminals/list":
            if (fake.ROOT / "mode").read_text() == "invalid-row":
                packet["result"] = {"data": ["unreadable worker"], "nextCursor": None}
        await self.socket.send(json.dumps(packet))
        if (mode in {"completion-on-resume", "bootstrap-recover-queued-completion"} and self.controller
                and self.methods.get(packet.get("id")) == "thread/resume"):
            turns = packet.get("result", {}).get("thread", {}).get("turns", [])
            if turns:
                turn = dict(turns[-1], status="completed")
                # Complete immediately after resume's active snapshot: the
                # controller must apply the response before later wire events.
                await self.socket.send(json.dumps({"method": "turn/completed", "params": {
                    "threadId": packet["result"]["thread"]["id"], "turn": turn}}))

    async def close(self, **kwargs):
        await self.socket.close(**kwargs)


original_request = fake.request


async def request(socket):
    await original_request(Socket(socket))


original_commands = fake.commands


async def extra_commands():
    path = fake.ROOT / "wire.jsonl"
    position = 0
    while True:
        if path.exists():
            with path.open() as stream:
                stream.seek(position)
                lines = stream.readlines()
                position = stream.tell()
            for line in lines:
                for client in list(fake.CLIENTS.values()):
                    await client["socket"].send(line)
        await asyncio.sleep(.02)


async def commands():
    await asyncio.gather(original_commands(), extra_commands())


fake.request = request
fake.commands = commands
fake.record("invoked", argv=sys.argv[1:])
asyncio.run(fake.backend() if "app-server" in sys.argv else fake.terminal())
