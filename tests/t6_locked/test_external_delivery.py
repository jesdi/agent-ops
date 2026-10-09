"""Actual public listener/supervisor/gateway/remote-terminal blackbox acceptance."""
import copy
import json
import unittest
from .external_fixture import ExternalFixture,eventually
from .contract_fixture import identity_key

class ExternalDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=ExternalFixture(name=self._testMethodName)
        self.addAsyncCleanup(self.flow.close)
        await self.flow.start()

    async def test_idle_completion_continues_same_root_while_other_worker_runs(self):
        a=await self.flow.command('generic A');b=await self.flow.command('generic B')
        await self.flow.qualify([a,b]);identity=await self.flow.finish(a)
        packet,record=await self.flow.result(identity)
        self.assertEqual(packet['method'],'turn/start');self.assertEqual(packet['params']['threadId'],self.flow.root)
        self.assertEqual(record['outcome'],{'status':'completed','exit_code':0,'aggregated_output':'generic output','duration_ms':0})
        self.assertTrue(any(w['identity'].get('initial_item_id')==b['item_id'] and w['status']=='running' for w in self.flow.view()['workers']))
        self.assertEqual(sum(entry['kind']=='client-request' and entry['payload']['method']=='thread/start' for entry in self.flow.records()),1)

    async def test_active_exact_steer_reaches_attached_operator_turn(self):
        command=await self.flow.command();await self.flow.qualify([command])
        operator=await self.flow.terminal('input',text='generic operator foreground',client_message_id='operator-foreground')
        current=operator['result']['turn']['id'];identity=await self.flow.finish(command,status='failed',exit_code=7,output='generic failure',duration=0)
        packet,record=await self.flow.result(identity)
        self.assertEqual(packet['method'],'turn/steer');self.assertEqual(packet['params']['expectedTurnId'],current)
        self.assertEqual(packet['params']['threadId'],self.flow.root)
        self.assertEqual(record['outcome']['exit_code'],7)
        self.assertEqual((await self.flow.terminal())['result']['thread']['turns'][-1]['id'],current)

    async def test_sent_reservation_is_durable_at_before_native_validation_barrier(self):
        command=await self.flow.command();await self.flow.qualify([command])
        await self.flow.terminal('input',text='generic active turn',client_message_id='operator-active')
        barrier=(await self.flow.control('barrier-arm',point='before-input-validation',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id']
        identity=await self.flow.finish(command)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T6_NO_PHYSICAL_DELIVERY_FORWARDING')
        packet,record=await self.flow.result(identity)
        client=packet['params']['clientUserMessageId'];view=self.flow.view()
        batch=next(batch for batch in view['deliveries'] if identity in batch['completion_ids']);attempt=next(a for a in batch['attempts'] if a['client_message_id']==client)
        self.assertEqual(attempt['status'],'sent-unconfirmed')
        self.assertEqual(view['inputs'][client],{'status':'pending','turn_id':None,'revision':attempt['admission_revision']})
        self.assertEqual(batch['input'],packet['params']['input'])
        await self.flow.control('barrier-release',barrier_id=barrier)

    async def test_independent_batches_operator_and_polling_progress_while_ack_held(self):
        a=await self.flow.command('A');b=await self.flow.command('B');await self.flow.qualify([a,b])
        foreground=await self.flow.terminal('input',text='generic active',client_message_id='operator-active')
        current=foreground['result']['turn']['id']
        barrier=(await self.flow.control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id']
        identity_a=await self.flow.finish(a)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T6_NO_FIRST_PHYSICAL_DELIVERY')
        first,record_a=await self.flow.result(identity_a);client_a=first['params']['clientUserMessageId']
        await self.flow.terminal('input',text='generic independent operator',client_message_id='operator-concurrent')
        identity_b=await self.flow.finish(b);second,record_b=await self.flow.result(identity_b)
        self.assertEqual(second['method'],'turn/steer');self.assertEqual(second['params']['expectedTurnId'],current)
        self.assertNotEqual(client_a,second['params']['clientUserMessageId'])
        await eventually(lambda:any(batch['status']=='confirmed' and identity_b in batch['completion_ids'] for batch in self.flow.view()['deliveries']),'T6_SECOND_ACTIVE_BATCH_BLOCKED_ON_FIRST_ACK')
        self.assertEqual(self.flow.view()['inputs'][client_a]['status'],'pending')
        self.assertTrue(any(w['identity']==identity_b and w['outcome']==record_b['outcome'] for w in self.flow.view()['workers']))
        await self.flow.control('barrier-release',barrier_id=barrier)
        await eventually(lambda:self.flow.view()['inputs'][client_a]['status'] in ['accepted','settled'],'T6_FIRST_ACK_NOT_CORRELATED')

    async def test_delayed_ack_a_after_stop_during_b_does_not_stop_b(self):
        command=await self.flow.command();await self.flow.qualify([command]);foreground=await self.flow.terminal('input',text='generic A',client_message_id='operator-a');turn_a=foreground['result']['turn']['id']
        barrier=(await self.flow.control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id']
        identity=await self.flow.finish(command)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T6_NO_DELIVERY_FOR_LATE_ACK')
        packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId']
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        await eventually(lambda:self.flow.view()['main']['status']=='stopped','SETUP_MATCHING_NORMAL_STOP_NOT_OBSERVED')
        recovered=(await self.flow.terminal())['result']['thread']
        self.assertEqual(recovered['turns'][-1]['id'],turn_a);self.assertEqual(recovered['turns'][-1]['status'],'completed')
        self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending')
        self.assertEqual(next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids'])['status'],'pending')
        next_prompt=await self.flow.terminal('input',text='generic B',client_message_id='operator-b');turn_b=next_prompt['result']['turn']['id']
        self.assertNotEqual(turn_a,turn_b);self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending')
        await self.flow.control('barrier-release',barrier_id=barrier)
        await eventually(lambda:self.flow.view()['inputs'][client]['status']=='settled','T6_LATE_ACK_A_NOT_SETTLED')
        self.assertEqual(self.flow.view()['main']['turn_id'],turn_b);self.assertEqual(self.flow.view()['main']['status'],'active')

    async def test_foreground_command_never_independently_redelivered(self):
        command=await self.flow.command();identity=await self.flow.finish(command)
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        await eventually(lambda:self.flow.view()['main']['status']=='stopped','SETUP_MAIN_COMPLETION_NOT_OBSERVED')
        self.assertFalse(any(record['identity']==identity for record in self.flow.view()['completions']))
        # A later eligible result is the non-vacuous positive sentinel.
        await self.flow.terminal('input',text='generic later work',client_message_id='operator-later')
        later=await self.flow.command();await self.flow.qualify([later]);later_id=await self.flow.finish(later);await self.flow.result(later_id)
        self.assertFalse(any(record['identity']==identity for _,records in self.flow.result_requests() for record in records))

    async def test_two_available_failures_batch_promptly_without_waiting_for_third(self):
        a=await self.flow.command('A');b=await self.flow.command('B');c=await self.flow.command('C');await self.flow.qualify([a,b,c])
        await self.flow.control('commands-complete-many',commands=[{'thread_id':self.flow.root,'item_id':a['item_id'],'status':'failed','exit_code':-1,'output':None,'duration_ms':None},{'thread_id':self.flow.root,'item_id':b['item_id'],'status':'completed','exit_code':0,'output':'','duration_ms':0}])
        identity_a={'kind':'command','thread_id':self.flow.root,'initial_item_id':a['item_id']};identity_b={'kind':'command','thread_id':self.flow.root,'initial_item_id':b['item_id']}
        packet,record=await self.flow.result(identity_a)
        records=next(records for candidate,records in self.flow.result_requests() if candidate==packet)
        self.assertCountEqual([r['identity'] for r in records],[identity_a,identity_b])
        self.assertIn({'identity':identity_a,'outcome':{'status':'failed','exit_code':-1,'aggregated_output':None,'duration_ms':None}},records)
        self.assertIn({'identity':identity_b,'outcome':{'status':'completed','exit_code':0,'aggregated_output':'','duration_ms':0}},records)
        self.assertTrue(any(w['identity'].get('initial_item_id')==c['item_id'] and w['status']=='running' for w in self.flow.view()['workers']))

    async def test_duplicate_native_completion_never_creates_another_submit(self):
        command=await self.flow.command();await self.flow.qualify([command]);identity=await self.flow.finish(command);await self.flow.result(identity)
        polls=sum(entry['kind']=='client-request' and entry['payload']['method']=='thread/backgroundTerminals/list' for entry in self.flow.records())
        for _ in range(2):await self.flow.control('duplicate-item-completion',thread_id=self.flow.root,item_id=command['item_id'])
        await eventually(lambda:sum(entry['kind']=='client-request' and entry['payload']['method']=='thread/backgroundTerminals/list' for entry in self.flow.records())>=polls+2,'SETUP_DUPLICATE_OBSERVATION_NOT_POLLED')
        self.assertEqual(sum(record['identity']==identity for _,records in self.flow.result_requests() for record in records),1)
        self.assertEqual(sum(record['identity']==identity for record in self.flow.view()['completions']),1)

    async def test_foreground_command_never_becomes_eligible_completion(self):
        command=await self.flow.command();identity=await self.flow.finish(command)
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        await eventually(lambda:self.flow.view()['main']['status']=='stopped' and self.flow.view()['inventory']=='known','SETUP_FOREGROUND_SCOPE_NOT_KNOWN')
        native=(await self.flow.native.rpc('thread/read',{'threadId':self.flow.root,'includeTurns':True}))['result']['thread']
        self.assertTrue(any(item['id']==command['item_id'] and item['status']=='completed' for turn in native['turns'] for item in turn['items'] if item['type']=='commandExecution'))
        self.assertFalse(any(w['identity']==identity and w['eligible'] for w in self.flow.view()['workers']))
        self.assertFalse(any(record['identity']==identity for record in self.flow.view()['completions']))

    async def history_message(self,status):
        child=(await self.flow.control('spawn-child',thread_id=self.flow.root))['thread_id']
        await self.flow.native.rpc('thread/resume',{'threadId':child})
        turn=(await self.flow.native.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'generic child work'}]}))['result']['turn']['id']
        identity={'kind':'agent','thread_id':child,'turn_id':turn}
        await eventually(lambda:any(w['identity']==identity and w['status']=='running' for w in self.flow.view()['workers']),'SETUP_OWNED_CHILD_NOT_DISCOVERED')
        await self.flow.control('history-items-empty',thread_id=child)
        await self.flow.control('complete-turn',thread_id=child,status=status,text='generic available child message',message_id='message-generic-child',error={'message':'generic child error','codexErrorInfo':None} if status=='failed' else None)
        completion=await eventually(lambda:next((r for r in self.flow.view()['completions'] if r['identity']==identity),None),'T6_NO_CHILD_COMPLETION_PUBLICATION')
        self.assertEqual(completion['outcome']['status'],status)
        self.assertIn({'item_id':'message-generic-child','text':'generic available child message'},completion['outcome']['messages'],'T6_SAFE_EXACT_TURN_MESSAGE_DISCARDED_BEFORE_PUBLICATION')
        packet,record=await self.flow.result(identity)
        self.assertEqual(record,completion)
        self.assertEqual(packet['params']['threadId'],self.flow.root)
        if status=='failed':self.assertEqual(record['outcome']['error']['message'],'generic child error')
    async def test_completed_child_preserves_message_from_successful_full_turn_history(self):await self.history_message('completed')
    async def test_failed_child_preserves_message_from_successful_full_turn_history(self):await self.history_message('failed')
    async def test_interrupted_child_preserves_message_from_successful_full_turn_history(self):await self.history_message('interrupted')

    async def test_depth_four_child_failure_and_foreign_root_are_isolated(self):
        foreign=(await self.flow.native.rpc('thread/start',{}))['result']['thread']['id']
        foreign_child=(await self.flow.control('spawn-child',thread_id=foreign))['thread_id']
        parent=self.flow.root
        nodes=[]
        for depth in range(1,5):
            child=(await self.flow.control('spawn-child',thread_id=parent))['thread_id'];nodes.append(child)
            await self.flow.native.rpc('thread/resume',{'threadId':child});turn=(await self.flow.native.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'generic descendant work'}]}))['result']['turn']['id']
            parent=child
        identity={'kind':'agent','thread_id':nodes[-1],'turn_id':turn}
        await eventually(lambda:any(w['identity']==identity and w['status']=='running' for w in self.flow.view()['workers']),'SETUP_DEPTH_FOUR_NOT_DISCOVERED')
        await self.flow.control('complete-turn',thread_id=nodes[-1],status='failed',text='generic nested failure message',error={'message':'generic depth-four failure','codexErrorInfo':None})
        packet,record=await self.flow.result(identity)
        self.assertEqual(record['outcome']['status'],'failed');self.assertEqual(record['outcome']['error']['message'],'generic depth-four failure')
        self.assertFalse(any(w['identity']['thread_id']==foreign_child for w in self.flow.view()['workers']))
        self.assertEqual(packet['params']['threadId'],self.flow.root)
        self.assertTrue(any(w['identity']['thread_id']==nodes[0] and w['status']=='running' for w in self.flow.view()['workers']))

    async def mismatch_race(self,idle):
        command=await self.flow.command();await self.flow.qualify([command]);before=await self.flow.terminal('input',text='generic old active turn',client_message_id='operator-before-race');old=before['result']['turn']['id']
        barrier=(await self.flow.control('barrier-arm',point='before-input-validation',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id']
        identity=await self.flow.finish(command)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T6_NO_DELIVERY_FOR_PREACCEPT_RACE')
        first,_=await self.flow.result(identity);client=first['params']['clientUserMessageId']
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        if not idle:
            current=(await self.flow.native.rpc('turn/start',{'threadId':self.flow.root,'input':[{'type':'text','text':'generic native concurrent prompt'}],'clientUserMessageId':'native-concurrent'}))['result']['turn']['id']
        await self.flow.control('barrier-release',barrier_id=barrier)
        await eventually(lambda:len([record for _,records in self.flow.result_requests() for record in records if record['identity']==identity])>=2,'T6_PROVEN_NONACCEPTANCE_NOT_RETRIED')
        attempts=[packet for packet,records in self.flow.result_requests() if any(record['identity']==identity for record in records)]
        self.assertEqual(len(attempts),2);self.assertNotEqual(attempts[1]['params']['clientUserMessageId'],client);self.assertEqual(attempts[0]['params']['input'],attempts[1]['params']['input'])
        if idle:self.assertEqual(attempts[1]['method'],'turn/start')
        else:self.assertEqual(attempts[1]['method'],'turn/steer');self.assertEqual(attempts[1]['params']['expectedTurnId'],current)
        receipts=[r for r in self.flow.records() if r['kind']=='native-accepted' and r['item'].get('clientId')==client];self.assertEqual(receipts,[])
        batch=next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch['attempts'][0]['status'],'rejected')
    async def test_proven_active_mismatch_retry_uses_fresh_turn_and_fresh_client(self):await self.mismatch_race(False)
    async def test_proven_idle_rejection_retry_continues_exact_root(self):await self.mismatch_race(True)

    async def uncertain_reply(self,mode):
        command=await self.flow.command();await self.flow.qualify([command]);await self.flow.terminal('input',text='generic active turn',client_message_id='operator-active')
        await self.flow.control('reply-fault-arm',method='turn/steer',mode=mode,code=-32601 if mode=='unsupported' else -32600,message='generic unsupported' if mode=='unsupported' else 'generic failure')
        identity=await self.flow.finish(command);packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId']
        await eventually(lambda:any(r['kind']=='intentional-after-acceptance-reply-fault' for r in self.flow.records()),'SETUP_REPLY_FAULT_NOT_EXERCISED')
        await self.flow.terminal('input',text='generic operator remains available',client_message_id='operator-after-error')
        polls=sum(r['kind']=='client-request' and r['payload']['method']=='thread/backgroundTerminals/list' for r in self.flow.records())
        await eventually(lambda:sum(r['kind']=='client-request' and r['payload']['method']=='thread/backgroundTerminals/list' for r in self.flow.records())>=polls+2,'T6_POLLING_STOPPED_ON_UNCERTAIN_ACK')
        self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending')
        batch=next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch['attempts'][0]['status'],'sent-unconfirmed')
        self.assertEqual(sum(record['identity']==identity for _,records in self.flow.result_requests() for record in records),1)
    async def test_generic_rpc_error_after_acceptance_stays_uncertain_without_resend(self):await self.uncertain_reply('generic')
    async def test_unsupported_rpc_error_after_acceptance_stays_uncertain_without_resend(self):await self.uncertain_reply('unsupported')
    async def test_malformed_ack_after_acceptance_stays_uncertain_without_resend(self):await self.uncertain_reply('malformed-result')

    async def test_two_tasks_share_listener_without_crossed_result_or_operator_input(self):
        other=ExternalFixture(name=self._testMethodName+'-other',issue=202,state_dir=self.flow.state);self.addAsyncCleanup(other.close);await other.start()
        command=await self.flow.command();await self.flow.qualify([command]);other_input=await other.terminal('input',text='generic other-task operator',client_message_id='operator-other-task')
        identity=await self.flow.finish(command);packet,record=await self.flow.result(identity)
        self.assertEqual(packet['params']['threadId'],self.flow.root);self.assertNotEqual(self.flow.root,other.root)
        self.assertEqual(other.result_requests(),[])
        other_history=(await other.terminal())['result']['thread']
        self.assertEqual(other_history['turns'][-1]['id'],other_input['result']['turn']['id'])
        self.assertTrue(any(item.get('clientId')=='operator-other-task' for turn in other_history['turns'] for item in turn['items']))
        self.assertFalse(any(item.get('clientId')=='operator-other-task' for turn in (await self.flow.terminal())['result']['thread']['turns'] for item in turn['items']))

    async def test_initial_missing_task_state_holds_result_until_fresh_publication(self):
        from dispatcher.state import save
        other=ExternalFixture(name=self._testMethodName+'-gap',issue=303,publish=False,state_dir=self.flow.state);self.addAsyncCleanup(other.close);await other.start()
        command=await other.command();await other.qualify([command]);identity=await other.finish(command)
        await eventually(lambda:any(r['identity']==identity for r in other.view()['completions']),'SETUP_GAP_COMPLETION_NOT_AVAILABLE')
        polls=sum(r['kind']=='client-request' and r['payload']['method']=='thread/backgroundTerminals/list' for r in other.records())
        await other.terminal('input',text='generic bootstrap gap operator still allowed',client_message_id='gap-operator')
        await eventually(lambda:sum(r['kind']=='client-request' and r['payload']['method']=='thread/backgroundTerminals/list' for r in other.records())>=polls+2,'SETUP_GAP_NOT_POLLED')
        self.assertEqual(other.result_requests(),[])
        save(other.state,other.task)
        packet,record=await other.result(identity);self.assertEqual(packet['params']['threadId'],other.root)

    async def test_supplementary_child_completion_does_not_repeat_main_submission(self):
        child=(await self.flow.control('spawn-child',thread_id=self.flow.root))['thread_id'];await self.flow.native.rpc('thread/resume',{'threadId':child})
        turn=(await self.flow.native.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'generic child work'}]}))['result']['turn']['id']
        identity={'kind':'agent','thread_id':child,'turn_id':turn}
        await eventually(lambda:any(w['identity']==identity and w['status']=='running' for w in self.flow.view()['workers']),'SETUP_CHILD_NOT_DISCOVERED')
        await self.flow.control('complete-turn',thread_id=child,status='failed',error={'message':'generic child failure','codexErrorInfo':None})
        packet,record=await self.flow.result(identity)
        self.assertEqual(record['outcome']['error']['message'],'generic child failure')
        self.assertEqual(record,{'identity':identity,'outcome':{'status':'failed','messages':[],'error':{'message':'generic child failure','codex_error_info':None}}})

        async def confirmed_result(result_identity,result_packet):
            client=result_packet['params']['clientUserMessageId']
            native=await eventually(lambda:next((r for r in self.flow.records() if r['kind']=='native-accepted' and r['thread_id']==self.flow.root and r['item'].get('clientId')==client),None),'T6_RESULT_NOT_NATIVELY_ACCEPTED')
            self.assertEqual(native['item']['content'],result_packet['params']['input'])
            def confirmed():
                view=self.flow.view();receipt=view['inputs'].get(client)
                batch=next((b for b in view['deliveries'] if result_identity in b['completion_ids'] and b['status']=='confirmed'),None)
                attempt=next((a for a in batch['attempts'] if a['client_message_id']==client and a['status']=='confirmed'),None) if batch else None
                return (batch,attempt,receipt) if attempt and receipt and receipt['status'] in ['accepted','settled'] and receipt['turn_id']==native['turn_id'] else None
            batch,attempt,receipt=await eventually(confirmed,'T6_RESULT_ACCEPTANCE_NOT_DURABLY_CONFIRMED')
            self.assertEqual(batch['input'],result_packet['params']['input'])
            self.assertEqual(attempt['thread_id'],self.flow.root)
            self.assertEqual(attempt['receipt']['thread_id'],self.flow.root)
            self.assertEqual(attempt['receipt']['turn_id'],native['turn_id'])
            self.assertGreater(attempt['admission_revision'],0)
            self.assertEqual(receipt['revision'],attempt['admission_revision'])
            return client

        original_client=await confirmed_result(identity,packet)
        await self.flow.control('duplicate-turn-completion',thread_id=child,turn_id=turn)
        # A later independent accepted result proves delivery progress after replay.
        sentinel=await self.flow.command('generic independent supplementary sentinel')
        await self.flow.qualify([sentinel])
        sentinel_identity={'kind':'command','thread_id':self.flow.root,'initial_item_id':sentinel['item_id']}
        self.assertTrue(any(w['identity']==sentinel_identity and w['eligible'] and w['status']=='running' for w in self.flow.view()['workers']))
        await eventually(lambda:self.flow.view()['inputs'][original_client]['status']=='settled','SETUP_ORIGINAL_RESULT_TURN_NOT_SETTLED')
        sentinel_identity=await self.flow.finish(sentinel,output='generic independent sentinel output')
        sentinel_packet,sentinel_record=await self.flow.result(sentinel_identity)
        self.assertEqual(sentinel_record,{'identity':sentinel_identity,'outcome':{'status':'completed','exit_code':0,'aggregated_output':'generic independent sentinel output','duration_ms':0}})
        sentinel_client=await confirmed_result(sentinel_identity,sentinel_packet)
        self.assertNotEqual(sentinel_client,original_client)
        self.assertTrue(any(r==sentinel_record for r in self.flow.view()['completions']))
        self.assertEqual(sum(r['identity']==identity for _,rs in self.flow.result_requests() for r in rs),1)
        self.assertEqual(sum(r['identity']==identity for r in self.flow.view()['completions']),1)
        accepted_records=[]
        for accepted in self.flow.records():
            if accepted['kind']!='native-accepted' or accepted['thread_id']!=self.flow.root:continue
            for item in accepted['item']['content']:
                try:payload=json.loads(item.get('text',''))
                except (ValueError,TypeError):continue
                if isinstance(payload,list) and payload and all(isinstance(r,dict) and set(r)=={'identity','outcome'} for r in payload):accepted_records.extend(payload)
        self.assertEqual([r for r in accepted_records if r['identity']==identity],[record])
        self.assertEqual([r for r in accepted_records if r['identity']==sentinel_identity],[sentinel_record])
