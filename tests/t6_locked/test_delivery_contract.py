"""Locked T6 acceptance using public events/views; never reads product bodies."""
import copy
import json
import unittest
from .contract_fixture import ContractFixture, result_input


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture()
        self.addCleanup(self.f.close)
        self.f.completion()

    def test_proposal_assigns_every_available_completion_and_exact_input(self):
        second=self.f.completion('command-b',status='failed',exit_code=-1,output=None,duration=None)
        event=self.f.propose()
        batch=self.f.batch()
        self.assertEqual(batch['input'],event['input'])
        self.assertCountEqual(batch['completion_ids'],[r['identity'] for r in self.f.records])
        self.assertEqual(batch['attempts'],[])
        self.assertEqual(batch['status'],'pending')
        self.assertIn(second,self.f.view()['completions'])

    def test_stale_proposal_is_atomic_then_fresh_proposal_succeeds(self):
        event=self.f.proposal(revision=self.f.view()['revision']-1)
        before=self.f.view()
        self.assertFalse(self.f.control.event(self.f.binding,event))
        self.assertEqual(before,self.f.view())
        self.f.propose()

    def test_partial_membership_rejected_without_assignment(self):
        self.f.completion('command-b')
        event=self.f.proposal(records=self.f.records[:1])
        before=self.f.view()
        self.assertFalse(self.f.control.event(self.f.binding,event))
        self.assertEqual(before,self.f.view())
        self.f.propose()

    def test_duplicate_membership_rejected_without_assignment(self):
        event=self.f.proposal(records=self.f.records*2)
        before=self.f.view()
        self.assertFalse(self.f.control.event(self.f.binding,event))
        self.assertEqual(before,self.f.view())
        self.f.propose()

    def test_replay_idempotent_but_conflicting_batch_immutable(self):
        event=self.f.propose()
        before=self.f.view()
        self.assertTrue(self.f.control.event(self.f.binding,event))
        self.assertEqual(before,self.f.view())
        altered=copy.deepcopy(event);altered['input'][0]['text']='changed explanatory text'
        self.assertFalse(self.f.control.event(self.f.binding,altered))
        self.assertEqual(before,self.f.view())

    def test_duplicate_inventory_cannot_reassign_existing_completion(self):
        event=self.f.propose()
        self.f.inventory()
        self.assertFalse(self.f.control.event(self.f.binding,self.f.proposal(batch='second')))
        self.assertEqual(len(self.f.view()['deliveries']),1)
        self.assertEqual(self.f.batch()['input'],event['input'])

    def test_sent_and_pending_existing_input_share_admission_revision(self):
        self.f.active();self.f.propose();self.f.send()
        view=self.f.view();attempt=self.f.batch()['attempts'][0]
        self.assertEqual(attempt['status'],'sent-unconfirmed')
        self.assertEqual(view['inputs']['client-generic'],{'status':'pending','turn_id':None,'revision':attempt['admission_revision']})
        self.assertEqual(attempt['admission_revision'],view['revision'])
        self.assertIn(view['main']['status'],{'active','unknown'})

    def test_new_distinct_active_batch_progresses_without_clearing_unresolved_first(self):
        self.f.active();self.f.propose();self.f.send()
        old=copy.deepcopy(self.f.batch())
        self.f.completion('command-b',status='failed',exit_code=7,output='failed result')
        self.f.apply({'type':'turn/recovered','thread_id':self.f.root,'turn_id':'main-b','status':'inProgress'})
        self.f.propose(batch='batch-two',records=self.f.records[-1:])
        self.f.send(batch='batch-two',attempt='attempt-two',client='client-two')
        self.f.ack(batch='batch-two',attempt='attempt-two',client='client-two')
        self.assertEqual(self.f.batch(),old)
        self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'pending')
        self.assertEqual(self.f.batch('batch-two')['status'],'confirmed')

    def test_idle_start_holds_operator_pending_then_active_steer_permits(self):
        self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-pending'))
        self.f.propose()
        event={'type':'delivery/sent','observed_revision':self.f.view()['revision'],'batch_id':'batch-generic','attempt_id':'idle-attempt','client_message_id':'idle-client','method':'turn/start','thread_id':self.f.root,'expected_turn_id':None}
        self.assertFalse(self.f.control.event(self.f.binding,event))
        self.assertNotIn('idle-client',self.f.view()['inputs'])
        self.f.active()
        self.f.send()
        self.assertEqual(self.f.view()['inputs']['operator-pending']['status'],'pending')

    def test_idle_start_holds_accepted_turn_owning_input(self):
        self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-pending'))
        self.f.apply({'type':'input/accepted','client_message_id':'operator-pending','turn_id':'operator-a'})
        self.f.propose()
        event={'type':'delivery/sent','observed_revision':self.f.view()['revision'],'batch_id':'batch-generic','attempt_id':'idle-attempt','client_message_id':'idle-client','method':'turn/start','thread_id':self.f.root,'expected_turn_id':None}
        self.assertFalse(self.f.control.event(self.f.binding,event))
        self.f.active('operator-a');self.f.send(expected='operator-a')

    def test_idle_result_wins_then_operator_admission_preserves_both_inputs(self):
        self.f.propose();self.f.send(method='turn/start',expected=None)
        self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-later'))
        self.f.ack(turn='main-next')
        self.assertEqual(self.f.view()['inputs']['operator-later']['status'],'pending')
        self.assertEqual(self.f.view()['inputs']['client-generic']['turn_id'],'main-next')

    def test_known_mismatch_rejects_only_attempt_and_allows_new_physical_attempt(self):
        self.f.active();event=self.f.propose();self.f.send()
        rejection={'type':'delivery/rejected','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','rejection':{'kind':'expected-active-turn','code':-32600,'message':'expected active turn id `main-b` but found `main-c`'}}
        self.assertTrue(self.f.control.event(self.f.binding,rejection))
        self.f.active('main-c');self.f.send(attempt='attempt-two',client='client-two',expected='main-c');self.f.ack(attempt='attempt-two',client='client-two',turn='main-c')
        batch=self.f.batch()
        self.assertEqual(batch['input'],event['input'])
        self.assertEqual(batch['completion_ids'],event['completion_ids'])
        self.assertEqual(batch['attempts'][0]['status'],'rejected')
        self.assertEqual(batch['attempts'][1]['status'],'confirmed')
        self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'rejected')

    def test_known_idle_rejection_requires_fresh_idle_state_and_resolved_barriers(self):
        self.f.active();self.f.propose();self.f.send()
        rejection={'type':'delivery/rejected','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','rejection':{'kind':'expected-active-turn','code':-32600,'message':'no active turn to steer'}}
        self.assertTrue(self.f.control.event(self.f.binding,rejection))
        self.f.stop()
        self.f.send(attempt='idle-attempt',client='idle-client',method='turn/start',expected=None)
        self.assertEqual(self.f.batch()['attempts'][-1]['method'],'turn/start')

    def test_generic_rejection_does_not_resolve_or_retry_sent_attempt(self):
        self.f.active();self.f.propose();self.f.send()
        before=self.f.view()
        for message in ['generic failure','no active turn to steer ','expected active turn id `foreign` but found `main-c`']:
            rejection={'type':'delivery/rejected','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','rejection':{'kind':'expected-active-turn','code':-32600,'message':message}}
            self.assertFalse(self.f.control.event(self.f.binding,rejection))
            self.assertEqual(self.f.view(),before)
        attempt={'type':'delivery/sent','observed_revision':self.f.view()['revision'],'batch_id':'batch-generic','attempt_id':'attempt-two','client_message_id':'client-two','method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'main-b'}
        self.assertFalse(self.f.control.event(self.f.binding,attempt))

    def test_ack_confirms_acceptance_without_synthesizing_main_stop(self):
        self.f.active();self.f.propose();self.f.send();before=self.f.view()['main'];self.f.ack()
        self.assertEqual(self.f.view()['main'],before)
        self.assertEqual(self.f.batch()['status'],'confirmed')
        self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'accepted')

    def test_normal_stop_before_ack_does_not_confirm_then_causal_ack_settles(self):
        self.f.active();self.f.propose();self.f.send();self.f.stop()
        self.assertEqual(self.f.batch()['status'],'pending')
        self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'pending')
        self.f.ack()
        self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'settled')
        self.assertEqual(self.f.view()['main']['status'],'stopped')

    def test_delayed_ack_a_during_active_b_settles_only_exact_a(self):
        self.f.active('main-b');self.f.propose();self.f.send();self.f.stop('main-b');self.f.active('main-c')
        self.assertTrue(self.f.control.accept_input(self.f.binding,'other-input'))
        self.f.apply({'type':'turn/recovered','thread_id':self.f.root,'turn_id':'main-c','status':'inProgress'})
        self.f.ack()
        view=self.f.view();self.assertEqual(view['main']['turn_id'],'main-c');self.assertEqual(view['main']['status'],'active')
        self.assertEqual(view['inputs']['client-generic']['status'],'settled')
        self.assertEqual(view['inputs']['other-input']['status'],'pending')

    def test_foreign_or_wrong_ack_cannot_free_attempt(self):
        self.f.active();self.f.propose();self.f.send();before=self.f.view()
        for field,value in [('thread_id','foreign'),('turn_id','foreign'),('batch_id','foreign'),('attempt_id','foreign'),('client_message_id','foreign')]:
            ack={'type':'delivery/ack','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','thread_id':self.f.root,'turn_id':'main-b'};ack[field]=value
            self.assertFalse(self.f.control.event(self.f.binding,ack));self.assertEqual(self.f.view(),before)
        self.f.ack()

    def test_receipt_replay_idempotent_and_late_rejection_cannot_undo_confirmation(self):
        self.f.active();self.f.propose();self.f.send();ack=self.f.ack();before=self.f.view()
        self.assertTrue(self.f.control.event(self.f.binding,ack));self.assertEqual(before,self.f.view())
        rejection={'type':'delivery/rejected','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','rejection':{'kind':'expected-active-turn','code':-32600,'message':'no active turn to steer'}}
        self.assertFalse(self.f.control.event(self.f.binding,rejection));self.assertEqual(before,self.f.view())

    def test_delivery_ack_preserves_wait_checkpoint_and_completion(self):
        checkpoint={'launch_id':self.f.binding['launch_id'],'seeded':True,'baseline_turns':[],'baseline_workers':[],'seen_completions':[r['identity'] for r in self.f.records],'scopes':[{'thread_id':self.f.root,'turn_id':None,'cursor':None,'complete':True}]}
        self.f.inventory(checkpoint=checkpoint);before=self.f.view();self.f.active();self.f.propose();self.f.send();self.f.ack();after=self.f.view()
        self.assertEqual(before['wait'],after['wait']);self.assertEqual(before['history_checkpoint'],after['history_checkpoint']);self.assertEqual(before['completions'],after['completions'])

    def test_pending_batch_and_uncertain_attempt_hold_ordinary_and_cap_retirement(self):
        self.f.propose()
        for reason in ['stopped','background']:
            self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason=reason,now=999999,cap=1),'held')
        self.f.active();self.f.send();self.f.stop()
        for reason in ['stopped','background']:
            self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason=reason,now=999999,cap=1),'held')

    def test_forced_retirement_fences_late_results_and_foreign_binding(self):
        self.f.propose();self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason='forced'),'retired')
        event=self.f.proposal(batch='late');self.assertFalse(self.f.control.event(self.f.binding,event))
        self.assertFalse(self.f.control.accept_input(self.f.binding,'late-input'))
        before=self.f.view();foreign=copy.deepcopy(self.f.binding);foreign['launch_id']='foreign-launch'
        self.assertFalse(self.f.control.event(foreign,event));self.assertEqual(before,self.f.view())

    def test_exact_latest_empty_stop_parks_after_confirmed_settled_result(self):
        self.f.active();self.f.propose();self.f.send();self.f.ack();self.f.stop()
        self.f.inventory()
        self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason='stopped'),'retired')

    def test_stale_send_does_not_create_pending_input_or_attempt(self):
        self.f.active();self.f.propose();before=self.f.view()
        event={'type':'delivery/sent','observed_revision':before['revision']-1,'batch_id':'batch-generic','attempt_id':'stale-attempt','client_message_id':'stale-client','method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'main-b'}
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
        self.f.send()

    def test_existing_operator_client_identity_cannot_be_reused_for_result(self):
        self.f.active();self.f.propose();self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-existing'));before=self.f.view()
        event={'type':'delivery/sent','observed_revision':before['revision'],'batch_id':'batch-generic','attempt_id':'attempt-reused','client_message_id':'operator-existing','method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'main-b'}
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
        self.f.send()

    def test_idle_read_recovery_alone_does_not_discharge_pending_input(self):
        self.f.propose();self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-unresolved'))
        pending=self.f.view()
        self.assertFalse(self.f.control.event(self.f.binding,{'type':'turn/recovered','thread_id':self.f.root,'turn_id':'main-a','status':'completed'}))
        self.assertEqual(self.f.view(),pending)
        before=self.f.view()
        event={'type':'delivery/sent','observed_revision':before['revision'],'batch_id':'batch-generic','attempt_id':'idle-attempt','client_message_id':'idle-client','method':'turn/start','thread_id':self.f.root,'expected_turn_id':None}
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
        self.assertEqual(before['inputs']['operator-unresolved']['status'],'pending')

    def test_completion_delivery_preserves_cap_equality_and_foreground_exemption(self):
        running=copy.deepcopy(self.f.workers[0]);running['identity']['initial_item_id']='running-sentinel';running.update(status='running',inventory_running=True,outcome=None)
        self.f.workers.append(running);self.f.inventory()
        wait=copy.deepcopy(self.f.view()['wait']);self.assertIsNotNone(wait)
        self.f.active();self.f.propose();self.f.send();self.f.ack()
        self.assertEqual(self.f.view()['wait'],wait)
        self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason='background',now=wait['since']+2,cap=1),'held')
        self.f.stop();self.f.inventory();view=self.f.view()
        self.assertEqual(self.f.control.retire(self.f.binding,view['revision'],reason='background',now=wait['since']+1,cap=1),'held')
        self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason='background',now=wait['since']+2,cap=1),'retired')


class SemanticPayloadTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close)
        self.f.completion(status='failed',exit_code=-1,output=None,duration=None)

    def reject_and_accept(self,content):
        before=self.f.view();event=self.f.proposal(content=content)
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(before,self.f.view());self.f.propose()

    def test_null_output_must_not_become_empty(self):
        records=copy.deepcopy(self.f.records);records[0]['outcome']['aggregated_output']='';self.reject_and_accept(result_input(records))

    def test_null_exit_must_not_become_zero(self):
        self.f.completion('command-b',status='declined',exit_code=None,output='',duration=0)
        records=copy.deepcopy(self.f.records);records[1]['outcome']['exit_code']=0;self.reject_and_accept(result_input(records))

    def test_missing_typed_identity_field_rejected(self):
        records=copy.deepcopy(self.f.records);records[0]['identity'].pop('initial_item_id');self.reject_and_accept(result_input(records))

    def test_invented_native_output_prefix_rejected(self):
        records=copy.deepcopy(self.f.records);records[0]['outcome']['aggregated_output']='invented lifetime output';self.reject_and_accept(result_input(records))

    def test_duplicate_payload_records_rejected(self):
        self.reject_and_accept(result_input(self.f.records*2))

    def test_missing_payload_record_rejected(self):
        self.reject_and_accept([{'type':'text','text':'[]'}])

    def test_two_machine_readable_payloads_rejected(self):
        self.reject_and_accept(result_input(self.f.records,False)*2)

    def test_json_envelope_is_not_the_declared_payload(self):
        self.reject_and_accept([{'type':'text','text':json.dumps({'completions':self.f.records})}])

    def test_malformed_json_and_prose_only_rejected(self):
        self.reject_and_accept([{'type':'text','text':'generic prose with invalid JSON ['}])

    def test_key_order_record_order_unicode_and_optional_prose_are_free(self):
        self.f.completion('command-b',output='generic λ\noutput',duration=0)
        records=[]
        for record in reversed(self.f.records):
            records.append({'outcome':dict(reversed(list(record['outcome'].items()))),'identity':dict(reversed(list(record['identity'].items())))})
        content=[{'type':'text','text':json.dumps(records,ensure_ascii=False,separators=(',',':'))},{'type':'text','text':'Other available results.'}]
        self.f.propose(content=content)
        self.assertEqual(self.f.batch()['input'],content)

    def test_boolean_exit_code_cannot_impersonate_native_zero(self):
        self.f.completion('command-zero',exit_code=0)
        records=copy.deepcopy(self.f.records);records[-1]['outcome']['exit_code']=False
        self.reject_and_accept(result_input(records))
