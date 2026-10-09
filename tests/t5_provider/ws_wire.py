"""Unix WebSocket JSON transport for the independent fixture.

Only the public WebSocket library is imported; no product modules or transports.
"""

import asyncio
import json

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed as Closed


class Connection:
    def __init__(self, wire):
        self.wire = wire
        self.lock = asyncio.Lock()

    async def send(self, packet):
        async with self.lock:
            await self.wire.send(json.dumps(packet, separators=(",", ":")))

    async def receive(self):
        return json.loads(await self.wire.recv())

    async def close(self):
        await self.wire.close()


async def serve(handler, path):
    async def connected(wire):
        await handler(Connection(wire))
    return await unix_serve(connected, path, max_size=16 * 1024 * 1024)


async def connect(path):
    return Connection(await unix_connect(path, max_size=16 * 1024 * 1024))


async def control(path, action):
    """Own fake-control socket only; never a product/runtime route."""
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps(action).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        if not result.get("ok"):
            raise ValueError(result.get("error", "fixture action failed"))
        return result.get("result")
    finally:
        writer.close()
        await writer.wait_closed()
