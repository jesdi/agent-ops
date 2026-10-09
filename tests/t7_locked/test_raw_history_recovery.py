"""Raw exact-root pages and actual native acceptances decide recovery, not local guesses."""
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow
from .state_observation import observe,assert_preserved

class RawHistoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def bad_history(self,method,mode):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory)
        # Several real input items force bounded opaque pagination independently of
        # production query limit/order. Their IDs are observed, never predicted.
        await self.flow.control('page-size-cap',limit=2)
        for i in range(6):await self.flow.terminal('input',text='Generic extra item '+str(i),client_message_id='page-item-'+str(i))
        await self.flow.exit(entry);await self.flow.control('complete-turn',thread_id=self.flow.root);await self.flow.terminal('input',text='Generic active B',client_message_id='operator-b')
        if mode=='malformed-page':await self.flow.control('persistent-malformed-items',enabled=True);fault=None
        else:fault=(await self.flow.control('history-fault-arm',method=method,thread_id=self.flow.root,mode=mode,**({'candidate_client_message_id':client} if mode in ['missing-next-cursor','repeat-cursor'] else {})))['fault_id']
        replacement=await self.flow.enter(factory)
        if mode=='missing-next-cursor':
            def observed_candidate_response():
                records=self.flow.records()
                for fault_record in records:
                    if fault_record['kind']!='history-observation-fault' or 'missing-next-cursor' not in fault_record['modes'] or fault_record['connection_id'] not in replacement['connections'] or not any(entry['turnId']==turn and entry['item'].get('clientId')==client and entry['item']['id']==self.r.accepted(client)[0]['item']['id'] for entry in fault_record['result']['data']):continue
                    requests=[r for r in records if r['kind']=='client-request' and r['connection_id']==fault_record['connection_id'] and r['payload']['method']==method and r['payload'].get('params',{})==fault_record['params'] and r['sequence']<fault_record['sequence']]
                    request=max(requests,key=lambda r:r['sequence'])
                    if any(r['kind']=='server-response' and r['connection_id']==request['connection_id'] and r['method']==method and type(r['payload']['id']) is type(request['payload']['id']) and r['payload']['id']==request['payload']['id'] and 'result' in r['payload'] and 'nextCursor' not in r['payload']['result'] and r['sequence']>fault_record['sequence'] for r in records):return fault_record
                return None
            candidate=await eventually(observed_candidate_response,'OWN_MALFORMED_PAGE_WITH_EXACT_D_NOT_CORRELATED');self.assertNotIn('nextCursor',candidate['result']);self.assertTrue(any(entry['turnId']==turn and entry['item'].get('clientId')==client and entry['item']['content']==[{**part,'text_elements':part.get('text_elements',[])} for part in self.r.accepted(client)[0]['item']['content']] for entry in candidate['result']['data']))
        if mode=='repeat-cursor':
            def observed_repeated_candidate_scope():
                records=self.flow.records()
                def response_for(observation):
                    requests=[r for r in records if r['kind']=='client-request' and r['connection_id']==observation['connection_id'] and r['payload']['method']==method and r['payload'].get('params',{})==observation['params'] and r['sequence']<observation['sequence']]
                    if not requests:return None
                    request=max(requests,key=lambda r:r['sequence'])
                    return next((r for r in records if r['kind']=='server-response' and r['connection_id']==request['connection_id'] and r['method']==method and type(r['payload']['id']) is type(request['payload']['id']) and r['payload']['id']==request['payload']['id'] and 'result' in r['payload'] and r['sequence']>observation['sequence']),None)
                for first in records:
                    if first['kind']!='history-observation-fault' or 'repeat-cursor' not in first['modes'] or first['connection_id'] not in replacement['connections'] or first['params'].get('cursor') or not first['result'].get('nextCursor') or not any(e['turnId']==turn and e['item']['id']==self.r.accepted(client)[0]['item']['id'] and e['item'].get('clientId')==client for e in first['result']['data']):continue
                    first_response=response_for(first)
                    if first_response is None:continue
                    for repeated in records:
                        if repeated['kind']!='history-observation-fault' or 'repeat-cursor' not in repeated['modes'] or repeated['connection_id']!=first['connection_id'] or repeated['sequence']<=first_response['sequence'] or repeated['params'].get('cursor')!=first['result']['nextCursor'] or repeated['result'].get('nextCursor')!=repeated['params'].get('cursor') or any(repeated['params'].get(key)!=first['params'].get(key) for key in ['threadId','turnId']) or (repeated['params'].get('sortDirection') or 'asc')!=(first['params'].get('sortDirection') or 'asc'):continue
                        if response_for(repeated) is not None:return first,repeated
                return None
            first,repeated=await eventually(observed_repeated_candidate_scope,'OWN_EXACT_D_PARTIAL_SCOPE_BEFORE_REPEATED_CURSOR_NOT_CORRELATED');self.assertTrue(any(e['turnId']==turn and e['item'].get('clientId')==client and e['item']['content']==[{**part,'text_elements':part.get('text_elements',[])} for part in self.r.accepted(client)[0]['item']['content']] for e in first['result']['data']));self.assertEqual(repeated['params']['cursor'],first['result']['nextCursor']);self.assertEqual(repeated['result']['nextCursor'],repeated['params']['cursor'])
        await self.r.condition(identity);await self.r.independent(other,client)
        await self.flow.exit(replacement);view=self.flow.view();batch=next(b for b in view['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch['status'],'pending');self.assertEqual(len(batch['attempts']),1);self.assertEqual(batch['input'],next(b for b in before['deliveries'] if identity in b['completion_ids'])['input']);self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1)
        observed=[r for r in self.flow.records() if (r['kind']=='history-observation-fault' and mode in r['modes']) or (mode=='malformed-page' and r['kind']=='intentional-after-acceptance-reply-fault' and r['mode']=='malformed-history')];self.assertTrue(observed,'OWN_HISTORY_FAULT_NOT_OBSERVED')
        if mode=='malformed-page':await self.flow.control('persistent-malformed-items',enabled=False)
        else:await self.flow.control('history-fault-clear',fault_id=fault)
        healthy=await self.flow.enter(factory);confirmed=await self.r.confirmed(identity);self.assertEqual(confirmed['attempts'][0]['receipt']['source'],'history');self.assertEqual(self.flow.view()['inputs'][client]['history_receipt']['turn_id'],turn);self.assertEqual(len(self.r.accepted(client)),1)
        await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(healthy)
    async def test_summary_native_turn_with_exact_d_cannot_settle_until_actual_full_view(self):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory)
        unrelated='summary-unrelated-a';self.assertIn('result',await self.flow.terminal('input',text='Generic real unrelated accepted A input',client_message_id=unrelated));self.assertEqual(len(self.r.accepted(unrelated)),1)
        omitted=self.r.accepted(unrelated)[0];self.assertEqual(omitted['turn_id'],turn);await self.flow.control('page-size-cap',limit=2)
        await self.flow.exit(entry);await self.flow.control('complete-turn',thread_id=self.flow.root)
        b_reply=await self.flow.terminal('input',text='Generic current B during summary A recovery',client_message_id='summary-operator-b');self.assertIn('result',b_reply);turn_b=b_reply['result']['turn']['id'];other_receipt=copy.deepcopy(self.flow.view()['inputs']['summary-operator-b'])
        faults=[(await self.flow.control('summary-turn-arm',method=method,thread_id=self.flow.root,summary_turn_id=turn,retain_client_message_id=client,omit_client_message_id=unrelated))['fault_id'] for method in ['thread/read','thread/turns/list']]
        replacement=await self.flow.enter(factory);await self.r.confirmed(identity)
        def summary_response():
            records=self.flow.records()
            for fault_record in records:
                if fault_record['kind']!='history-observation-fault' or 'summary-turn' not in fault_record['modes'] or fault_record['connection_id'] not in replacement['connections']:continue
                result=fault_record['result'];turns=result['thread']['turns'] if fault_record['method']=='thread/read' else result['data']
                reduced=next((t for t in turns if t['id']==turn and t['itemsView']=='summary' and t['status']=='completed' and t['error'] is None and any(item.get('clientId')==client for item in t['items']) and not any(item['id']==omitted['item']['id'] for item in t['items'])),None)
                if reduced is None:continue
                requests=[r for r in records if r['kind']=='client-request' and r['connection_id']==fault_record['connection_id'] and r['payload']['method']==fault_record['method'] and r['payload'].get('params',{})==fault_record['params'] and r['sequence']<fault_record['sequence']]
                if not requests:continue
                request=max(requests,key=lambda r:r['sequence'])
                if any(r['kind']=='server-response' and r['method']==fault_record['method'] and r['connection_id']==request['connection_id'] and type(r['payload']['id']) is type(request['payload']['id']) and r['payload']['id']==request['payload']['id'] and 'result' in r['payload'] and r['sequence']>fault_record['sequence'] for r in records):return reduced
            return None
        reduced=await eventually(summary_response,'OWN_SCHEMA_VALID_REDUCED_A_WITH_EXACT_D_NOT_RETURNED');self.assertTrue(any(item['id']==self.r.accepted(client)[0]['item']['id'] for item in reduced['items']));self.assertNotIn(omitted['item']['id'],[item['id'] for item in reduced['items']])
        await eventually(lambda:self.flow.view()['main']['turn_id']==turn_b and self.flow.view()['main']['status']=='active','T7_SUMMARY_SETUP_DID_NOT_OBSERVE_ACTIVE_B');authority=copy.deepcopy(self.flow.view());state_before=observe(self.flow)
        self.assertIn('result',await self.flow.terminal('input',text='Generic independent summary-view progress',client_message_id='summary-progress'));other_identity=await self.flow.finish(other);await self.r.confirmed(other_identity)
        view=self.flow.view();receipt=view['inputs'][client];batch=next(b for b in view['deliveries'] if identity in b['completion_ids'])
        self.assertEqual(receipt['status'],'accepted','T7_SUMMARY_NATIVE_A_MUST_NOT_SETTLE_EXACT_D');self.assertNotIn('history_settlement',receipt);self.assertEqual(receipt['history_receipt']['turn_id'],turn);self.assertEqual(receipt['revision'],before['inputs'][client]['revision']);self.assertEqual('native_input' in receipt,'native_input' in before['inputs'][client]);self.assertEqual(receipt.get('native_input'),before['inputs'][client].get('native_input'))
        self.assertEqual(view['inputs']['summary-operator-b'],other_receipt);self.assertEqual(view['main'],authority['main']);self.assertEqual(view['wait'],authority['wait']);self.assertNotIn(turn,view['main']['completed_turns']);assert_preserved(self,self.flow,state_before)
        self.assertEqual(batch['input'],next(b for b in before['deliveries'] if identity in b['completion_ids'])['input']);self.assertEqual(len(batch['attempts']),1);self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1)
        accepted_receipt=copy.deepcopy(receipt);await self.flow.exit(replacement)
        for fault in faults:await self.flow.control('history-fault-clear',fault_id=fault)
        healthy=await self.flow.enter(factory);await eventually(lambda:self.flow.view()['inputs'][client]['status']=='settled','T7_ACTUAL_FULL_A_DID_NOT_SETTLE_EXACT_D');after=self.flow.view();settled=after['inputs'][client]
        self.assertEqual(settled['revision'],accepted_receipt['revision']);self.assertEqual('native_input' in settled,'native_input' in accepted_receipt);self.assertEqual(settled.get('native_input'),accepted_receipt.get('native_input'));self.assertEqual(settled['history_receipt'],accepted_receipt['history_receipt']);self.assertEqual(settled['history_settlement']['turn_id'],turn);self.assertEqual(settled['history_settlement']['item_id'],self.r.accepted(client)[0]['item']['id'])
        self.assertEqual(after['inputs']['summary-operator-b'],other_receipt);self.assertEqual(after['main'],authority['main']);self.assertEqual(after['wait'],authority['wait']);self.assertNotIn(turn,after['main']['completed_turns']);assert_preserved(self,self.flow,state_before);self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1)
        await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(healthy)
    async def test_two_distinct_native_items_with_attempted_client_id_remain_ambiguous_without_provider_dedup(self):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory)
        duplicate=await self.flow.native.rpc('turn/steer',packet['params']);self.assertEqual(duplicate['result']['turnId'],turn);accepted=self.r.accepted(client);self.assertEqual(len(accepted),2);self.assertNotEqual(accepted[0]['item']['id'],accepted[1]['item']['id'])
        await self.flow.exit(entry);await self.flow.control('complete-turn',thread_id=self.flow.root);await self.flow.terminal('input',text='Generic active B after ambiguous duplicate',client_message_id='operator-b');replacement=await self.flow.enter(factory);await self.r.condition(identity);await self.r.independent(other,client);await self.flow.exit(replacement)
        batch=next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch['status'],'pending');self.assertEqual(len(batch['attempts']),1);self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending');self.assertEqual(len(self.r.accepted(client)),2);await self.flow.control('barrier-release',barrier_id=barrier)
    async def test_repeated_same_item_observation_is_idempotent_after_exact_history_confirmation(self):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory);await self.flow.exit(entry);await self.flow.control('complete-turn',thread_id=self.flow.root);await self.flow.terminal('input',text='Generic active B',client_message_id='operator-b');replacement=await self.flow.enter(factory);batch=await self.r.confirmed(identity)
        receipt=copy.deepcopy(self.flow.view()['inputs'][client]);attempts=copy.deepcopy(batch['attempts']);await self.flow.exit(replacement);again=await self.flow.enter(factory)
        self.assertIn('result',await self.flow.terminal('input',text='Generic duplicate scan progress',client_message_id='duplicate-scan-operator'));other_identity=await self.flow.finish(other);await self.r.confirmed(other_identity)
        self.assertEqual(self.flow.view()['inputs'][client],receipt);self.assertEqual(next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids'])['attempts'],attempts);self.assertEqual(len(self.r.accepted(client)),1);self.assertEqual(len(self.r.records_for(identity)),1);await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(again)

def fault_case(method,mode):
    async def test(self):await self.bad_history(method,mode)
    return test
for name,mode in [('missing_cursor','missing-next-cursor'),('repeated_cursor','repeat-cursor'),('missing_client','missing-client'),('null_client','null-client'),('changed_exact_text','mismatched-text'),('wrong_owning_turn','mismatched-turn'),('unreadable_items','error'),('malformed_items','malformed-page')]:setattr(RawHistoryRecoveryTests,'test_'+name+'_keeps_d_held_while_e_and_operator_progress',fault_case('thread/items/list',mode))
