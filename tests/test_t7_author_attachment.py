"""Cancellation is observed at the real Unix listener boundary."""
import asyncio
import json
import unittest

from dispatcher.codex_supervisor import attach_controller
from dispatcher.runtime_http import BoundClient
from tests.t7_locked.composition_fixture import CompositionFixture
from tests.t7_locked.http_fault_proxy import message


class HeldListenerResponse:
    def __init__(self, state, upstream):
        self.path = state / 'wait' / 'wait.sock'
        self.upstream = str(upstream)
        self.hit, self.release = asyncio.Event(), asyncio.Event()
        self.handlers = set()

    async def start(self):
        self.path.parent.mkdir(parents=True)
        self.server = await asyncio.start_unix_server(self.serve, path=str(self.path))

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        upstream = None
        try:
            header, body = await message(reader)
            request = json.loads(body)
            source, upstream = await asyncio.open_unix_connection(self.upstream)
            upstream.write(header + body)
            await upstream.drain()
            response, data = await message(source)
            if request.get('event', {}).get('type') == 'turn/recovered':
                self.hit.set()
                await self.release.wait()
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
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.gather(*self.handlers)
        self.path.unlink(missing_ok=True)


class AttachmentListenerQuiescenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_root_with_no_input_admission_can_finish_first_launch_once(self):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        proxy = await flow.use_proxy()
        rule = proxy.arm(event_type='bound', route='/runtime/event')
        entry = await flow.enter(attach_controller, mode='first-launch', ready=False)
        from tests.t7_locked.external_fixture import eventually
        await eventually(lambda: proxy.hit(rule), 'Correlated binding response was not intercepted')
        try:
            await flow.exit(entry)
        except RuntimeError:
            pass
        before = flow.view()
        self.assertEqual(before['bootstrap']['root_status'], 'bound')
        self.assertEqual(before['inputs'], {})
        replacement = await flow.enter(attach_controller, mode='first-launch')
        self.assertEqual(flow.view()['bootstrap'], before['bootstrap'])
        initial = before['bootstrap']['initial_input']['client_message_id']
        self.assertEqual(flow.view()['inputs'][initial]['status'], 'accepted')
        self.assertEqual(sum(r['kind'] == 'native-accepted' for r in flow.records()), 1)
        await flow.exit(replacement)

    async def test_exit_awaits_inflight_listener_response_while_gateway_remains_usable(self):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        await flow.first_launch(attach_controller)
        await flow.start_terminal()
        proxy_state = flow.directory / 'held-listener'
        proxy = HeldListenerResponse(proxy_state, flow.state / 'wait' / 'wait.sock')
        await proxy.start()
        self.addAsyncCleanup(proxy.close)
        bound = BoundClient(proxy_state, flow.target, flow.issue, flow.binding['launch_id'])
        context = attach_controller(bound, flow.binding, flow.arguments,
            backend_path=flow.backend_path, input_lock=flow.gateway.input_lock)
        handle = await context.__aenter__()
        closing = None
        try:
            await asyncio.wait_for(proxy.hit.wait(), 3)
            closing = asyncio.create_task(context.__aexit__(None, None, None))
            response = await flow.terminal('input', text='Gateway remains available during closure.',
                                           client_message_id='while-listener-held')
            self.assertIn('result', response)
            self.assertFalse(closing.done(), 'Context exited while its listener invocation was still held')
        finally:
            proxy.release.set()
            if closing is None:
                await context.__aexit__(None, None, None)
            else:
                await closing
        await handle.wait_closed()
        with self.assertRaises(RuntimeError):
            await handle.wait_ready()
        self.assertIsNone(flow.backend.poll())
        self.assertIsNone(flow.supervisor.poll())
