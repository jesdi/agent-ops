"""Accepted bound starts release the terminal independently of receipt recovery."""
import asyncio
import unittest

from dispatcher.codex_supervisor import attach_controller
from tests.t7_locked.composition_fixture import CompositionFixture
from tests.t7_locked.external_fixture import eventually


class LifecycleReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_started_initial_turn_is_ready_while_ack_and_history_are_unavailable(self):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        barrier = (await flow.control('root-reply-barrier-arm', method='thread/start'))['barrier_id']
        entry = await flow.enter(attach_controller, mode='first-launch', ready=False)
        async def hit():
            return (await flow.control('root-reply-barrier-status', barrier_id=barrier))['hits'] > 0
        await eventually(hit, 'Root barrier missing')
        row = next(r for r in flow.records() if r['kind'] == 'root-reply-barrier-hit')
        await flow.control('hide-active-receipts', enabled=True)
        await flow.control('scoped-fault-arm', point='after-acceptance-before-ack', method='turn/start',
                           connection_id=row['connection_id'], mode='drop-reply')
        await flow.control('root-reply-barrier-release', barrier_id=barrier)
        await eventually(lambda: flow.view()['main']['status'] == 'active', 'Bound start missing')
        await flow.ready(entry, timeout=2)
        snapshot = flow.view()
        self.assertEqual([r['status'] for r in snapshot['inputs'].values()], ['pending'])
        self.assertEqual(snapshot['main']['completed_turns'], {})
        self.assertFalse(snapshot['retired'])
        self.assertEqual(sum(r['kind'] == 'native-accepted' for r in flow.records()), 1)
        self.assertIsNone(flow.backend.poll())
        await flow.exit(entry)
        replacement = await flow.enter(attach_controller, ready=False)
        await flow.ready(replacement, timeout=2)
        self.assertEqual(flow.view()['inputs'], snapshot['inputs'])
        await flow.exit(replacement)
        await flow.start_terminal()
        owner = await flow.enter(attach_controller)
        response = await flow.terminal('input', text='A later ordinary foreground steer.',
                                       client_message_id='later-operator')
        self.assertIn('result', response)
        await flow.exit(owner)
        later = await flow.enter(attach_controller, ready=False)
        await flow.ready(later, timeout=2)
        initial_id = snapshot['bootstrap']['initial_input']['client_message_id']
        self.assertEqual(flow.view()['inputs'][initial_id]['status'], 'pending')
        await flow.control('persistent-malformed-items', enabled=True)
        await flow.control('complete-turn', thread_id=flow.root)
        await eventually(lambda: flow.view()['main']['status'] == 'stopped', 'Initial Stop missing')
        response = await flow.terminal('input', text='Start the next foreground turn.',
                                       client_message_id='next-operator')
        self.assertIn('result', response)
        await eventually(lambda: flow.view()['main']['status'] == 'active', 'Later main turn missing')
        await flow.exit(later)
        newest = await flow.enter(attach_controller, ready=False)
        await flow.ready(newest, timeout=2)
        self.assertEqual(flow.view()['inputs'][initial_id]['status'], 'pending')
        self.assertEqual(flow.view()['main']['initial_start'], snapshot['main']['initial_start'])
        await flow.exit(newest)

    async def test_default_attachment_requires_new_accepted_bound_start_for_pending_initial(self):
        flow = CompositionFixture(name=self._testMethodName)
        self.addAsyncCleanup(flow.close)
        await flow.start()
        root = (await flow.native.rpc('thread/start', dict(cwd=str(flow.worktree))))['result']['thread']['id']
        before = flow.view()
        content = [dict(type='text', text=flow.arguments.prompt)]
        assert flow.host.event(flow.binding, dict(type='bootstrap/root-attempted',
            observed_revision=before['revision'], root_operation_id='operation', root_method='thread/start',
            requested_conversation_id=None, client_message_id='initial', input=content))
        assert flow.host.event(flow.binding, dict(type='bound', root_operation_id='operation', conversation_id=root))
        flow.binding = flow.view()['binding']
        assert flow.host.event(flow.binding, dict(type='turn/started', thread_id=root, turn_id='stale'))
        assert flow.host.event(flow.binding, dict(type='turn/completed', thread_id=root, turn_id='stale', status='completed'))
        assert flow.host.event(flow.binding, dict(type='bootstrap/sent', root_operation_id='operation',
            client_message_id='initial', thread_id=root, observed_revision=flow.view()['revision']))
        await flow.control('hide-active-receipts', enabled=True)
        entry = await flow.enter(attach_controller, ready=False)
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(entry['handle'].wait_ready(), .4)
        foreign = (await flow.native.rpc('thread/start', {}))['result']['thread']['id']
        await flow.native.rpc('turn/start', dict(threadId=foreign, input=content))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(entry['handle'].wait_ready(), .4)
        await flow.native.rpc('turn/start', dict(threadId=root, clientUserMessageId='initial', input=content))
        await flow.ready(entry, timeout=2)
        self.assertEqual(flow.view()['inputs']['initial']['status'], 'pending')
        self.assertEqual(set(flow.view()['main']['completed_turns']), {'stale'})
        await flow.exit(entry)
