"""Own public flow setup shared by raw history recovery acceptance cases."""
import copy
from .external_fixture import eventually

class RecoveryFlow:
    def __init__(self,case,flow):self.case=case;self.flow=flow
    def factory(self):
        factory=self.flow.factory();self.case.assertTrue(callable(factory),'T7_ATTACHMENT_UNAVAILABLE_AFTER_HEALTHY_PUBLIC_SETUP');return factory
    def accepted(self,client):return [r for r in self.flow.records() if r['kind']=='native-accepted' and r['item'].get('clientId')==client]
    def records_for(self,identity):return [r for _,records in self.flow.result_requests() for r in records if r['identity']==identity]
    async def confirmed(self,identity):
        return await eventually(lambda:next((b for b in self.flow.view()['deliveries'] if identity in b['completion_ids'] and b['status']=='confirmed'),None),'T7_RECOVERY_BATCH_NOT_CONFIRMED')
    async def condition(self,identity):
        def pending():
            view=self.flow.view();batch=next((b for b in view['deliveries'] if identity in b['completion_ids']),None)
            return next((alert for alert in view['alerts'] if batch and alert.get('kind')=='delivery-uncertain' and alert['batch_id']==batch['batch_id'] and alert['status']=='pending'),None)
        return await eventually(pending,'T7_UNCERTAINTY_NOT_DURABLY_OBSERVED')
    async def held(self,factory):
        entry=await self.flow.ready_composition(factory);a=await self.flow.command('generic uncertain worker');other=await self.flow.command('generic independent eligible worker');await self.flow.qualify([a,other])
        foreground=await self.flow.terminal('input',text='Generic owner A',client_message_id='operator-a');turn=foreground['result']['turn']['id'];barrier=(await self.flow.control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id'];identity=await self.flow.finish(a)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_NATIVE_RESULT_ACK_NOT_HELD');packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId'];batch=next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']);self.case.assertEqual(batch['attempts'][0]['status'],'sent-unconfirmed');self.case.assertEqual(self.flow.view()['inputs'][client]['status'],'pending');self.case.assertEqual(len(self.accepted(client)),1)
        return entry,barrier,identity,packet,client,turn,other,copy.deepcopy(self.flow.view())
    async def independent(self,other,client_d):
        operator=await self.flow.terminal('input',text='Generic independent active input',client_message_id='independent-operator');self.case.assertIn('result',operator)
        identity=await self.flow.finish(other);batch=await self.confirmed(identity);self.case.assertEqual(batch['attempts'][0]['method'],'turn/steer');self.case.assertEqual(self.flow.view()['inputs'][client_d]['status'],'pending');return identity,batch
