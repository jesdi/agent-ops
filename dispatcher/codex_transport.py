"""Codex JSON-RPC connections and a transparent, per-terminal Unix gateway."""
import asyncio
from contextlib import asynccontextmanager
import json

from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed


class ProtocolError(RuntimeError):
    """A required RPC was rejected or returned an unreadable payload."""


class RPC:
    def __init__(self, socket, notification):
        self.socket = socket
        self.notification = notification
        self.pending = {}
        self.sequence = 0

    async def call(self, method, params, *, on_result=None):
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = (future, on_result)
        try:
            await self.socket.send(json.dumps(dict(id=identity, method=method, params=params)))
            response = await asyncio.wait_for(future, timeout=10)
            if "error" in response:
                raise ProtocolError(f"{method}: {response['error']}")
            return response["result"]
        finally:
            self.pending.pop(identity, None)

    async def receive(self):
        try:
            async for raw in self.socket:
                await self._packet(json.loads(raw))
        finally:
            for future, _ in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("control connection closed"))

    async def _packet(self, packet):
        if not isinstance(packet, dict):
            raise ProtocolError("invalid JSON-RPC packet")
        if "method" not in packet:
            entry = self.pending.get(packet.get("id"))
            if entry is not None:
                await self._response(entry, packet)
        elif "id" not in packet:
            await self.notification(packet)
        # Server requests belong to the real terminal's connection. Never
        # answer an operator approval from the controller.


    async def _response(self, entry, packet):
        future, on_result = entry
        if future.done():
            return
        # Recovery snapshots must be applied before subsequent notifications
        # on this ordered stream, not later when the calling task wakes up.
        if on_result is not None and "result" in packet:
            await on_result(packet["result"])
        future.set_result(packet)


@asynccontextmanager
async def connect(path, notification):
    async with unix_connect(path=path) as socket:
        rpc = RPC(socket, notification)
        receiver = asyncio.create_task(rpc.receive())
        try:
            await rpc.call("initialize", {
                "clientInfo": {"name": "agent-ops", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await socket.send(json.dumps({"method": "initialized"}))
            yield rpc
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


class Gateway:
    """Preserve bidirectional RPC IDs, notifications and server requests.

    Each terminal has its own backend connection. Controller reconnection
    cannot replace that connection or swallow its response/approval traffic.
    The input lock is shared with controller submissions for later admission.
    """
    def __init__(self, backend_path, conversation):
        self.backend_path = backend_path
        self.conversation = conversation
        self.input_lock = asyncio.Lock()

    async def serve(self, terminal):
        try:
            async with unix_connect(path=self.backend_path) as backend:
                tasks = [asyncio.create_task(self._upstream(terminal, backend)),
                         asyncio.create_task(self._downstream(backend, terminal))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (OSError, ConnectionClosed):
            await terminal.close()

    async def _downstream(self, backend, terminal):
        async for raw in backend:
            await terminal.send(raw)

    async def _upstream(self, terminal, backend):
        async for raw in terminal:
            packet = json.loads(raw)
            method = packet.get("method", "")
            params = packet.get("params") or {}
            if self._foreign(method, params):
                await terminal.send(json.dumps({"id": packet.get("id"), "error": {
                    "code": -32602, "message": "terminal is bound to one conversation"}}))
            elif method in ("turn/start", "turn/steer"):
                async with self.input_lock:
                    await backend.send(raw)
            else:
                await backend.send(raw)

    def _foreign(self, method, params):
        if method == "thread/start":
            return True
        if method == "thread/resume" or method.startswith("turn/"):
            return params.get("threadId") != self.conversation
        return False
