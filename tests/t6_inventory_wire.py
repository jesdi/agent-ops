"""Owned native wire faults for inventory reads; real listener/provider retained."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import sys

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tests/t6_locked'))
from provider import Provider
from codex_fake import terminal


class InventoryFault:
    def __init__(self, run):
        self.run = run
        self.prepared = False
        self.fired = False

    def arm(self):
        path = self.run / 'inventory-fault.json'
        return json.loads(path.read_text()) if path.exists() else None

    async def before(self, request):
        if self.fired and request.get('method') == 'thread/list':
            (self.run / 'inventory-next-poll.json').write_text(json.dumps(request))
            while not (self.run / 'inventory-release').exists():
                await asyncio.sleep(.01)

    def reply(self, request, packet):
        arm = self.arm()
        if not arm or self.fired or 'result' not in packet:
            return packet
        method, params = request.get('method'), request.get('params', {})
        owner = params.get('threadId') == arm['owner']
        if arm['mode'] == 'retained' and method == 'thread/list':
            result = deepcopy(packet)
            result['result']['data'] = [row for row in result['result']['data'] if row['id'] != arm['owner']]
            self.save('inventory-owner-hidden.json', request, packet, result)
            self.prepared = True
            return result
        if arm['mode'] == 'resume' and owner and method == 'thread/read' and not self.prepared:
            result = deepcopy(packet)
            result['result']['thread']['status'] = {'type': 'notLoaded'}
            self.save('inventory-not-loaded.json', request, packet, result)
            self.prepared = True
            return result
        matches = {
            'read': method == 'thread/read' and params.get('includeTurns') is True,
            'retained': self.prepared and method == 'thread/read' and params.get('includeTurns') is False,
            'resume': self.prepared and method == 'thread/resume',
        }
        if owner and matches[arm['mode']]:
            result = {'id': packet['id'], 'result': arm['result']}
            self.save('inventory-fault-reply.json', request, packet, result)
            self.fired = True
            return result
        return packet

    def save(self, name, request, original, replaced):
        (self.run / name).write_text(json.dumps({
            'request': request, 'original': original, 'replaced': replaced}, indent=2))


async def main():
    args = sys.argv[1:]
    run = Path(os.environ['T6_FIXTURE_RUN_DIR'])
    if args[0] == '--remote':
        await terminal(Path(args[1][7:]), args[6], run)
        return
    assert args[:2] == ['app-server', '--listen']
    native_path = Path(args[2][7:])
    upstream_path = run / 'inventory-upstream.sock'
    provider = Provider(run, native_socket=str(upstream_path))
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, provider.stopped.set)
    service = asyncio.create_task(provider.serve())
    await provider.ready.wait()
    fault = InventoryFault(run)

    async def proxy(downstream):
        async with unix_connect(str(upstream_path)) as upstream:
            requests = {}

            async def up():
                async for raw in downstream:
                    request = json.loads(raw)
                    if 'id' in request:
                        requests[request['id']] = request
                    await fault.before(request)
                    await upstream.send(raw)

            async def down():
                async for raw in upstream:
                    packet = json.loads(raw)
                    if 'method' not in packet:
                        request = requests.pop(packet.get('id'), {})
                        packet = fault.reply(request, packet)
                    await downstream.send(json.dumps(packet))

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async with unix_serve(proxy, str(native_path)):
        await provider.stopped.wait()
    await service


if __name__ == '__main__':
    asyncio.run(main())
