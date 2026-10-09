"""Exact own receipt settlement and fresh native idle allow continuation without main Stop."""
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow
from .state_observation import observe,assert_preserved

class MissedStopIdleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def offline_idle(self,factory):
        entry=await self.flow.ready_composition(factory);work=await self.flow.command('generic idle recovered outcome');running=await self.flow.command('generic known running worker');await self.flow.qualify([work,running]);turn=(await self.flow.terminal('input',text='Generic main A',client_message_id='operator-a'))['result']['turn']['id']
        # This foreground command has no recorded current main owning Stop.
        foreground=await self.flow.command('generic nonqualified foreground');before=copy.deepcopy(self.flow.view());self.assertEqual(before['main']['status'],'active');self.assertEqual(before['inputs']['operator-a']['status'],'accepted');await self.flow.exit(entry)
        await self.flow.control('complete-turn',thread_id=self.flow.root);identity=await self.flow.finish(work);await self.flow.finish(foreground);return identity,turn,before,foreground
    async def test_receipt_local_missed_stop_with_fresh_exact_idle_admits_same_root_start_without_lifecycle_rewrite(self):
        factory=self.r.factory();identity,turn,before,foreground=await self.offline_idle(factory);state_before=observe(self.flow);barrier=(await self.flow.control('barrier-arm',point='before-input-validation',method='turn/start',thread_id=self.flow.root,one_shot=True))['barrier_id'];entry=await self.flow.enter(factory)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_MISSED_STOP_NATIVE_IDLE_DID_NOT_ADMIT_CONTINUATION');packet,_=await self.flow.result(identity);self.assertEqual(packet['method'],'turn/start');view=self.flow.view();self.assertEqual(view['main'],before['main']);self.assertNotIn(turn,view['main']['completed_turns']);self.assertEqual(view['inputs']['operator-a']['status'],'settled');self.assertEqual(view['inputs']['operator-a']['history_settlement']['turn_id'],turn)
        self.assertEqual(view['wait'],before['wait']);self.assertEqual(view['history_checkpoint']['baseline_turns'],before['history_checkpoint']['baseline_turns']);self.assertFalse(any(c['identity'].get('initial_item_id')==foreground['item_id'] for c in view['completions']));self.assertFalse(any(w.get('eligible') and w['identity'].get('initial_item_id')==foreground['item_id'] for w in view['workers']))
        self.assertEqual(self.flow.sessions.runtime_view(self.flow.target,self.flow.issue)['main'],before['main']);client=packet['params']['clientUserMessageId'];self.assertEqual(view['inputs'][client]['status'],'pending');self.assertEqual(len(self.r.records_for(identity)),1)
        assert_preserved(self,self.flow,state_before)
        await self.flow.control('barrier-release',barrier_id=barrier);await self.r.confirmed(identity);self.assertEqual(len(self.r.accepted(client)),1);await self.flow.exit(entry)
    async def test_unrelated_pending_input_blocks_idle_continuation_until_its_independent_rejection(self):
        factory=self.r.factory();identity,turn,before,foreground=await self.offline_idle(factory);self.assertTrue(self.flow.bound.accept_input(self.flow.binding,'unrelated-unforwarded-input'));entry=await self.flow.enter(factory)
        await eventually(lambda:self.flow.view()['inputs']['operator-a']['status']=='settled','T7_OWN_MISSED_STOP_INPUT_NOT_SETTLED');view=self.flow.view();self.assertEqual(view['inputs']['unrelated-unforwarded-input']['status'],'pending');self.assertFalse(self.r.records_for(identity));self.assertEqual(view['main'],before['main'])
        self.assertTrue(self.flow.bound.event(self.flow.binding,{'type':'input/rejected','client_message_id':'unrelated-unforwarded-input'}));await self.r.confirmed(identity);self.assertEqual(len(self.r.records_for(identity)),1);await self.flow.exit(entry)
    async def test_foreign_current_read_is_not_idle_authority_then_healthy_exact_read_can_continue(self):
        factory=self.r.factory();identity,turn,before,foreground=await self.offline_idle(factory);fault=(await self.flow.control('history-fault-arm',method='thread/read',thread_id=self.flow.root,mode='foreign-thread'))['fault_id'];entry=await self.flow.enter(factory)
        self.assertIn('result',await self.flow.native.rpc('thread/read',{'threadId':self.flow.root,'includeTurns':True}));await self.flow.exit(entry);self.assertFalse(self.r.records_for(identity));self.assertEqual(self.flow.view()['main'],before['main'])
        await self.flow.control('history-fault-clear',fault_id=fault);healthy=await self.flow.enter(factory);await self.r.confirmed(identity);self.assertEqual(len(self.r.records_for(identity)),1);await self.flow.exit(healthy)
