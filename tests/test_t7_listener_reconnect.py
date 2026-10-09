"""Listener wire outages must preserve an attached controller's recovery lifetime."""
import asyncio
import copy
import json
import unittest

from dispatcher.codex_supervisor import attach_controller
from dispatcher.runtime_http import BoundClient
from tests.t7_locked.composition_fixture import CompositionFixture
from tests.t7_locked.external_fixture import eventually
from tests.t7_locked.http_fault_proxy import message


class ListenerOutage:
    """Forward real HTTP until one selected view and its diagnostic lose transport."""
    def __init__(self, state, upstream, view_number):
        self.path = state / 'wait' / 'wait.sock'
        self.upstream = str(upstream)
        self.view_number = view_number
        self.views = 0
        self.failed_view = asyncio.Event()
        self.failed_diagnostic = asyncio.Event()
        self.restored = False
        self.handlers = set()
        self.records = []

    async def start(self):
        self.path.parent.mkdir(parents=True)
        self.server = await asyncio.start_unix_server(self.serve, path=str(self.path))

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        upstream = None
        try:
            header, body = await message(reader)
            route = header.split(b' ', 2)[1].decode()
            request = json.loads(body)
            self.records.append({'route': route, 'request': request})
            if route == '/runtime/view':
                self.views += 1
                if self.views >= self.view_number and not self.restored:
                    self.failed_view.set()
                    return
            if request.get('event', {}).get('type') == 'control/unknown' and not self.restored:
                self.failed_diagnostic.set()
                return
            source, upstream = await asyncio.open_unix_connection(self.upstream)
            upstream.write(header + body)
            await upstream.drain()
            response, data = await message(source)
            writer.write(response + data)
            await writer.drain()
        finally:
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self):
        self.restored = True
        self.server.close()
        await self.server.wait_closed()
        await asyncio.gather(*self.handlers)
        self.path.unlink(missing_ok=True)


class ListenerReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def recovery_after_outage(self, view_number):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        entry = await flow.ready_composition(attach_controller)
        command = await flow.command('Result whose native acknowledgment is lost')
        other = await flow.command('Independent running work')
        await flow.qualify([command, other])
        foreground = await flow.terminal('input', text='Active A', client_message_id='outage-operator-a')
        turn_a = foreground['result']['turn']['id']
        barrier = (await flow.control('barrier-arm', point='after-acceptance-before-ack',
            method='turn/steer', thread_id=flow.root, one_shot=True))['barrier_id']
        identity = await flow.finish(command)
        async def accepted():
            return (await flow.control('barrier-status', barrier_id=barrier))['hits'] > 0
        await eventually(accepted, 'Result did not reach its native acceptance barrier')
        packet, _ = await flow.result(identity)
        client = packet['params']['clientUserMessageId']
        await flow.exit(entry)
        before = copy.deepcopy(flow.view())
        self.assertEqual(before['inputs'][client]['status'], 'pending')
        await flow.control('complete-turn', thread_id=flow.root)
        response = await flow.terminal('input', text='Independent active B', client_message_id='outage-operator-b')
        turn_b = response['result']['turn']['id']
        backend, terminal, gateway, lock = flow.backend.pid, flow.supervisor.pid, flow.gateway, flow.gateway.input_lock
        state = flow.directory / 'outage-listener'
        proxy = ListenerOutage(state, flow.state / 'wait' / 'wait.sock', view_number)
        await proxy.start()
        self.addAsyncCleanup(proxy.close)
        bound = BoundClient(state, flow.target, flow.issue, flow.binding['launch_id'])
        manager = attach_controller(bound, flow.binding, flow.arguments,
            backend_path=flow.backend_path, input_lock=lock)
        handle = await manager.__aenter__()
        closed = asyncio.create_task(handle.wait_closed())
        diagnostic = asyncio.create_task(proxy.failed_diagnostic.wait())
        recovered = False
        try:
            await asyncio.wait_for(proxy.failed_view.wait(), 3)
            done, _ = await asyncio.wait((closed, diagnostic), timeout=3,
                                         return_when=asyncio.FIRST_COMPLETED)
            self.assertNotIn(closed, done, 'Listener outage closed the public attachment')
            self.assertIn(diagnostic, done, 'Unavailable-listener diagnostic was not attempted')
            self.assertEqual(flow.view()['inputs'][client]['status'], 'pending')
            proxy.restored = True
            try:
                recovered_binding = await asyncio.wait_for(handle.wait_ready(), 5)
            except ConnectionError as error:
                self.fail(f'Listener diagnostic outage terminated recovery: {error}')
            self.assertEqual(recovered_binding, flow.binding)
            await eventually(lambda: flow.view()['inputs'][client]['status'] == 'settled',
                             'Same attachment failed to recover exact accepted D history')
            after = flow.view()
            batch = next(b for b in after['deliveries'] if identity in b['completion_ids'])
            self.assertEqual(batch['status'], 'confirmed')
            self.assertEqual(len(batch['attempts']), 1)
            self.assertEqual(after['inputs'][client]['history_settlement']['turn_id'], turn_a)
            self.assertEqual(after['main']['turn_id'], turn_b)
            self.assertEqual(after['main']['status'], 'active')
            self.assertNotIn(turn_a, after['main']['completed_turns'])
            self.assertEqual(after['bootstrap'], before['bootstrap'])
            self.assertEqual(after['history_checkpoint']['baseline_turns'], before['history_checkpoint']['baseline_turns'])
            self.assertEqual(after['wait']['since'], before['wait']['since'])
            self.assertEqual(flow.backend.pid, backend)
            self.assertEqual(flow.supervisor.pid, terminal)
            self.assertIs(flow.gateway, gateway)
            self.assertIs(flow.gateway.input_lock, lock)
            self.assertIsNone(flow.backend.poll())
            self.assertIsNone(flow.supervisor.poll())
            self.assertEqual(sum(r['kind'] == 'native-accepted' and r['item'].get('clientId') == client for r in flow.records()), 1)
            self.assertEqual(sum(r['kind'] == 'client-request' and r['payload']['method'] == 'thread/start' for r in flow.records()), 1)
            flow.phase('listener-outage-recovered', view_number=view_number, proxy_requests=proxy.records,
                       before=before, after=after, client_message_id=client)
            await flow.control('barrier-release', barrier_id=barrier)
            recovered = True
        finally:
            proxy.restored = True
            diagnostic.cancel()
            await asyncio.gather(diagnostic, return_exceptions=True)
            # A failing observer is already quiescent; still exit its public owner
            # before the fixture's independent backend/terminal cleanup.
            exit_results = await asyncio.gather(manager.__aexit__(None, None, None), return_exceptions=True)
            close_results = await asyncio.gather(closed, return_exceptions=True)
            flow.phase('listener-outage-cleanup', proxy_requests=proxy.records, recovered=recovered,
                       exit_errors=[repr(r) for r in exit_results + close_results if isinstance(r, BaseException)])
            if recovered:
                self.assertEqual(exit_results + close_results, [False, None])

    async def test_initial_observer_view_outage_recovers_same_delivery(self):
        await self.recovery_after_outage(2)

    async def test_connected_view_and_diagnostic_outage_recovers_same_delivery(self):
        await self.recovery_after_outage(3)
