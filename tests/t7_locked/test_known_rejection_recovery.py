"""Native proved nonacceptance alone permits a freshly gated distinct attempt."""
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow

class KnownRejectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def race(self,idle):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);command=await self.flow.command('generic proved rejection');await self.flow.qualify([command]);a=(await self.flow.terminal('input',text='Generic race A',client_message_id='race-operator-a'))['result']['turn']['id'];barrier=(await self.flow.control('barrier-arm',point='before-input-validation',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id'];identity=await self.flow.finish(command)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_KNOWN_REJECTION_PREACCEPT_BARRIER_NOT_HIT');old_packet,_=await self.flow.result(identity);old_client=old_packet['params']['clientUserMessageId'];self.assertEqual(self.r.accepted(old_client),[]);await self.flow.control('complete-turn',thread_id=self.flow.root)
        if not idle:b=(await self.flow.terminal('input',text='Generic race B',client_message_id='race-operator-b'))['result']['turn']['id']
        await self.flow.control('barrier-release',barrier_id=barrier);batch=await self.r.confirmed(identity);attempts=batch['attempts'];self.assertEqual(len(attempts),2);self.assertEqual(attempts[0]['status'],'rejected');self.assertNotEqual(attempts[0]['attempt_id'],attempts[1]['attempt_id']);self.assertNotEqual(old_client,attempts[1]['client_message_id']);self.assertEqual(len(self.r.accepted(old_client)),0);self.assertEqual(len(self.r.accepted(attempts[1]['client_message_id'])),1)
        self.assertEqual(attempts[1]['method'],'turn/start' if idle else 'turn/steer');self.assertEqual(attempts[1]['expected_turn_id'],None if idle else b);rejected=[r for r in self.flow.records() if r['kind']=='native-rejected' and r['method']=='turn/steer'];self.assertTrue(any(r['payload']['error']=={'code':-32600,'message':'no active turn to steer' if idle else f'expected active turn id `{a}` but found `{b}`'} for r in rejected));self.assertEqual(len(self.r.records_for(identity)),2);await self.flow.exit(entry)
    async def test_obsolete_active_expected_turn_proved_rejection_allows_one_fresh_active_attempt(self):await self.race(False)
    async def test_obsolete_expected_turn_with_native_idle_proved_rejection_allows_one_fresh_start(self):await self.race(True)
