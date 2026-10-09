"""Owned wire fault provider adapter. Product and locked source stay untouched."""
import asyncio
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
from dispatcher.runtime_http import RuntimeClient


async def main():
    args = sys.argv[1:]
    run = Path(os.environ['T6_FIXTURE_RUN_DIR'])
    if args[0] == '--remote':
        await terminal(Path(args[1][7:]), args[6], run)
        return
    assert args[:2] == ['app-server', '--listen']
    native_path = Path(args[2][7:])
    upstream_path = run / 'actual-native.sock'
    provider = Provider(run, native_socket=str(upstream_path))
    loop = asyncio.get_running_loop()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, provider.stopped.set)
    servicing = asyncio.create_task(provider.serve())
    await provider.ready.wait()
    fired = False

    async def proxy(downstream):
        nonlocal fired
        async with unix_connect(str(upstream_path)) as upstream:
            requests = {}

            async def up():
                nonlocal fired
                async for raw in downstream:
                    packet = json.loads(raw)
                    requests[packet.get('id')] = packet
                    if packet.get('method') == 'thread/read' and not fired and (run / 'arm-root-fault.json').exists():
                        host = RuntimeClient(os.environ['AGENT_OPS_STATE_DIR'])
                        snapshot = host.view(os.environ['AGENT_OPS_TARGET'], int(os.environ['AGENT_OPS_ISSUE']), os.environ['AGENT_OPS_LAUNCH_ID'])
                        if snapshot and any(b['status'] == 'pending' and not b['attempts'] for b in snapshot.get('deliveries', [])):
                            fired = True
                            packet['_owned_fault_result'] = json.loads((run / 'arm-root-fault.json').read_text())['result']
                            (run / 'wire-fault-selection.json').write_text(json.dumps({'request': {k:v for k,v in packet.items() if k != '_owned_fault_result'}, 'snapshot_before': snapshot}, indent=2))
                    await upstream.send(raw)

            async def down():
                async for raw in upstream:
                    packet = json.loads(raw)
                    request = requests.pop(packet.get('id'), {}) if 'method' not in packet else {}
                    if '_owned_fault_result' in request:
                        before = packet
                        packet = {'id': packet['id'], 'result': request['_owned_fault_result']}
                        (run / 'wire-fault-reply.json').write_text(json.dumps({'original_reply': before, 'fault_reply': packet}, indent=2))
                        raw = json.dumps(packet)
                    await downstream.send(raw)

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async with unix_serve(proxy, str(native_path)):
        await provider.stopped.wait()
    await servicing


if __name__ == '__main__': asyncio.run(main())
