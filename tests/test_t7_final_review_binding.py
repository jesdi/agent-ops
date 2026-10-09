"""A lost listener reply preserves the one live managed root operation."""
import unittest

from dispatcher.codex_supervisor import attach_controller
from dispatcher.runtime_control import RuntimeControl
from tests.t7_locked.composition_fixture import CompositionFixture
from tests.t7_locked.external_fixture import eventually


class LostBoundReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_attachment_recovers_committed_bind_without_second_root_or_input(self):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        assert flow.host.event(flow.binding, dict(type='service', status='unknown'))
        proxy = await flow.use_proxy()
        rule = proxy.arm(event_type='bound', route='/runtime/event')
        entry = await flow.enter(attach_controller, mode='first-launch', ready=False)
        await eventually(lambda: proxy.hit(rule), 'Bound response was not dropped')
        bound = flow.view()
        self.assertEqual(bound['bootstrap']['root_status'], 'bound')
        await flow.ready(entry)
        after = flow.view()
        self.assertEqual(after['bootstrap'], bound['bootstrap'])
        self.assertEqual(after['binding'], bound['binding'])
        self.assertEqual(after['service'], 'live')
        self.assertIsNone(flow.backend.poll())
        requests = [r for r in flow.records() if r['kind'] == 'client-request']
        self.assertEqual(sum(r['payload']['method'] == 'thread/start' for r in requests), 1)
        self.assertEqual(sum(r['kind'] == 'native-accepted' for r in flow.records()), 1)
        await flow.exit(entry)


def test_declared_root_operation_requires_recorded_attempt_but_legacy_bind_remains(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review')['binding']
    before = control.view('owned', 17)
    assert not control.event(binding, dict(type='bound', conversation_id='root', root_operation_id='unattempted'))
    assert control.view('owned', 17) == before
    assert control.event(binding, dict(type='bound', conversation_id='legacy-root'))
    assert control.view('owned', 17)['binding']['conversation_id'] == 'legacy-root'
    assert control.view('owned', 17)['bootstrap'] is None


class CorruptedRefinedView:
    """A listener transport fault; the underlying durable listener stays unchanged."""
    def __init__(self, client, field):
        self.client, self.field, self.dropped = client, field, False

    def event(self, binding, event):
        result = self.client.event(binding, event)
        if event['type'] == 'bound' and result:
            self.dropped = True
            raise ConnectionError('Lost committed bound reply')
        return result

    def view(self):
        from copy import deepcopy
        snapshot = deepcopy(self.client.view())
        if not self.dropped:
            return snapshot
        if self.field == 'operation':
            snapshot['bootstrap']['root_operation_id'] = 'foreign-operation'
        elif self.field == 'input':
            snapshot['bootstrap']['initial_input']['input'][0]['text'] = 'Foreign initial input'
        elif self.field == 'root':
            snapshot['binding']['conversation_id'] = 'foreign-root'
        elif self.field == 'launch':
            snapshot['binding']['launch_id'] = 'foreign-launch'
        elif self.field == 'dead':
            snapshot['service'] = 'dead'
        elif self.field == 'retired':
            snapshot['retired'] = True
        elif self.field == 'attempt-revision':
            snapshot['bootstrap']['root_attempt_revision'] -= 1
        return snapshot


class RootCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_bind_reply_cannot_adopt_uncorrelated_or_closed_public_view(self):
        import asyncio
        for field in ('operation', 'input', 'root', 'launch', 'dead', 'retired', 'attempt-revision', 'resume-operation'):
            with self.subTest(field=field):
                flow = CompositionFixture(name='bind-correlation-' + field)
                await flow.start()
                if field == 'resume-operation':
                    from dispatcher.runtime_http import BoundClient
                    root = (await flow.native.rpc('thread/start', {}))['result']['thread']['id']
                    flow.binding = flow.host.prepare(flow.target, flow.issue, flow.stage,
                        worktree=str(flow.worktree), conversation_id=root)['binding']
                    assert flow.host.event(flow.binding, dict(type='service', status='live'))
                    flow.bound = BoundClient(flow.state, flow.target, flow.issue, flow.binding['launch_id'])
                client = CorruptedRefinedView(flow.bound, 'operation' if field == 'resume-operation' else field)
                try:
                    with self.assertRaises(RuntimeError):
                        async with attach_controller(client, flow.binding, flow.arguments,
                                backend_path=flow.backend_path, input_lock=asyncio.Lock(),
                                mode='first-launch') as attachment:
                            await asyncio.wait_for(attachment.wait_ready(), 2)
                    snapshot = flow.view()
                    self.assertEqual(snapshot['inputs'], {})
                    self.assertEqual(snapshot['service'], 'live')
                    self.assertIsNone(flow.backend.poll())
                    self.assertEqual(sum(r['kind'] == 'native-accepted' for r in flow.records()), 0)
                finally:
                    await flow.close()


class AttachmentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_attach_does_not_publish_live_and_dead_launch_stays_dead(self):
        import asyncio
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        await flow.first_launch(attach_controller)
        assert flow.host.event(flow.binding, dict(type='service', status='unknown'))
        entry = await flow.enter(attach_controller)
        self.assertEqual(flow.view()['service'], 'unknown')
        await flow.exit(entry)
        assert flow.host.event(flow.binding, dict(type='service', status='dead'))
        before = flow.view()
        requests = [r for r in flow.records() if r['kind'] == 'client-request']
        for mode in ('attach', 'first-launch'):
            with self.assertRaises(RuntimeError):
                async with attach_controller(flow.bound, flow.binding, flow.arguments,
                        backend_path=flow.backend_path, input_lock=asyncio.Lock(), mode=mode) as attachment:
                    await attachment.wait_ready()
        self.assertEqual(flow.view(), before)
        self.assertEqual([r for r in flow.records() if r['kind'] == 'client-request'], requests)
        self.assertIsNone(flow.backend.poll())


def test_empty_root_cannot_bind_or_consume_managed_attempt(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review')['binding']
    before = control.view('owned', 17)
    assert not control.event(binding, dict(type='bound', conversation_id=''))
    assert control.view('owned', 17) == before
    assert control.event(binding, dict(type='bootstrap/root-attempted',
        observed_revision=before['revision'], root_operation_id='operation', root_method='thread/start',
        requested_conversation_id=None, client_message_id='initial', input=[dict(type='text', text='Initial')]))
    attempted = control.view('owned', 17)
    assert not control.event(binding, dict(type='bound', conversation_id=None, root_operation_id='operation'))
    assert control.view('owned', 17) == attempted
    assert control.event(binding, dict(type='bound', conversation_id='native-root', root_operation_id='operation'))
    bound = control.view('owned', 17)
    assert bound['binding']['conversation_id'] == 'native-root'
    assert bound['bootstrap']['root_status'] == 'bound'
    assert bound['bootstrap']['initial_input'] == attempted['bootstrap']['initial_input']
