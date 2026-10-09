"""Actual remote TUI/Gateway reserves immutable ordinary provenance before native send."""
import asyncio
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow

async def cancel(task):task.cancel();await asyncio.gather(task,return_exceptions=True)

class GatewayOrdinaryProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def test_actual_gateway_pending_exact_text_provenance_exists_before_native_validation(self):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);await self.flow.control('complete-turn',thread_id=self.flow.root);content=[{'type':'text','text':' Exact λ\n'},{'type':'text','text':''}];client='ordinary-durable-text';barrier=(await self.flow.control('barrier-arm',point='before-input-validation',method='turn/start',thread_id=self.flow.root,client_message_id=client,one_shot=True))['barrier_id'];before_input_revision=self.flow.view()['revision'];pending=asyncio.create_task(self.flow.terminal('input-native',method='turn/start',input=content,client_message_id=client));self.addAsyncCleanup(cancel,pending)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_ORDINARY_NATIVE_FORWARD_NOT_OBSERVED');view=self.flow.view();receipt=view['inputs'][client];self.assertEqual(receipt['status'],'pending');self.assertIsNone(receipt['turn_id']);self.assertEqual(receipt['native_input'],{'method':'turn/start','thread_id':self.flow.root,'expected_turn_id':None,'input':content});self.assertGreater(receipt['revision'],before_input_revision);self.assertLessEqual(receipt['revision'],view['revision']);self.assertEqual(self.r.accepted(client),[])
        before=copy.deepcopy(receipt);await self.flow.control('barrier-release',barrier_id=barrier);self.assertIn('result',await pending);after=self.flow.view()['inputs'][client];self.assertEqual(after['native_input'],before['native_input']);self.assertEqual(after['revision'],before['revision']);self.assertEqual(after['status'],'accepted');self.assertEqual(len(self.r.accepted(client)),1);await self.flow.exit(entry)
    async def test_lost_ordinary_text_ack_recovers_existing_receipt_without_new_admission_or_resend(self):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);await self.flow.control('complete-turn',thread_id=self.flow.root);client='ordinary-lost-text';content=[{'type':'text','text':' Exact λ\n'}];await self.flow.control('hide-active-receipts',enabled=True)
        await self.flow.control('scoped-fault-arm',point='after-acceptance-before-ack',method='turn/start',client_message_id=client,mode='disconnect');pending=asyncio.create_task(self.flow.terminal('input-native',method='turn/start',input=content,client_message_id=client));self.addAsyncCleanup(cancel,pending)
        accepted=await eventually(lambda:next(iter(self.r.accepted(client)),None),'T7_ORDINARY_LOST_ACK_NOT_ACCEPTED');before=copy.deepcopy(self.flow.view()['inputs'][client]);self.assertEqual(before['status'],'pending');self.assertEqual(before['native_input']['input'],content);await self.flow.exit(entry);await cancel(pending)
        await self.flow.control('complete-turn',thread_id=self.flow.root);replacement=await self.flow.enter(factory);await eventually(lambda:self.flow.view()['inputs'][client]['status']=='settled','T7_ORDINARY_EXACT_HISTORY_NOT_SETTLED');after=self.flow.view()['inputs'][client];self.assertEqual(after['revision'],before['revision']);self.assertEqual(after['native_input'],before['native_input']);self.assertEqual(after['history_receipt']['item_id'],accepted['item']['id']);self.assertEqual(len(self.r.accepted(client)),1);await self.flow.exit(replacement)
    async def test_richer_native_operator_input_keeps_actual_admission_forward_and_ack_access(self):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);content=[{'type':'text','text':'λ','text_elements':[{'byteRange':{'start':0,'end':2},'placeholder':'generic'}]},{'type':'image','url':'https://example.invalid/generic'}];client='ordinary-richer'
        response=await self.flow.terminal('input-native',method='turn/start',input=content,client_message_id=client);self.assertIn('result',response);accepted=self.r.accepted(client);self.assertEqual(len(accepted),1);self.assertEqual(accepted[0]['item']['content'],content);receipt=self.flow.view()['inputs'][client];self.assertEqual(receipt['status'],'accepted')
        if 'native_input' in receipt:self.assertEqual(receipt['native_input']['input'],content)
        await self.flow.exit(entry)
