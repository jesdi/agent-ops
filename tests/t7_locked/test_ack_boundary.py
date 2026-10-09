"""Only exact correlated ACK or later exact receipt resolves actual native uncertainty."""
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow

class NativeACKBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def uncertain_reply(self,mode,code=None):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);command=await self.flow.command('generic ACK uncertainty');other=await self.flow.command('generic ACK sentinel');await self.flow.qualify([command,other]);turn=(await self.flow.terminal('input',text='Generic active target',client_message_id='operator-a'))['result']['turn']['id']
        fields={'method':'turn/steer','mode':mode}
        if code is not None:fields.update(code=code,message='generic unclassified native error')
        await self.flow.control('reply-fault-arm',**fields);identity=await self.flow.finish(command);packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId'];await self.r.condition(identity);before=copy.deepcopy(self.flow.view());self.assertEqual(before['inputs'][client]['status'],'pending');self.assertEqual(len(self.r.accepted(client)),1)
        await self.r.independent(other,client);await self.flow.exit(entry);self.assertEqual(len(self.r.records_for(identity)),1);batch=next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch['status'],'pending');self.assertEqual(len(batch['attempts']),1)
        await self.flow.control('complete-turn',thread_id=self.flow.root);replacement=await self.flow.enter(factory);confirmed=await self.r.confirmed(identity);self.assertEqual(confirmed['attempts'][0]['receipt']['source'],'history');self.assertEqual(self.flow.view()['inputs'][client]['history_receipt']['turn_id'],turn);self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1);await self.flow.exit(replacement)
    async def test_schema_valid_wrong_turn_success_is_uncertain_not_accepted_or_retried(self):await self.uncertain_reply('wrong-turn-success')
    async def test_malformed_success_is_uncertain_not_accepted_or_retried(self):await self.uncertain_reply('malformed-result')
    async def test_generic_invalid_request_after_native_acceptance_is_not_known_rejection(self):await self.uncertain_reply('error',-32600)
    async def test_unsupported_error_after_native_acceptance_is_not_known_rejection(self):await self.uncertain_reply('error',-32601)
    async def test_lost_socket_ack_after_native_acceptance_recovers_only_exact_original_attempt(self):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory);await self.flow.exit(entry);await self.flow.control('complete-turn',thread_id=self.flow.root);replacement=await self.flow.enter(factory);confirmed=await self.r.confirmed(identity)
        self.assertEqual(len(confirmed['attempts']),1);self.assertEqual(confirmed['attempts'][0]['attempt_id'],next(b for b in before['deliveries'] if identity in b['completion_ids'])['attempts'][0]['attempt_id']);self.assertEqual(confirmed['attempts'][0]['receipt']['source'],'history');self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1);await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(replacement)
