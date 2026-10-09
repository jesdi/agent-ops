"""Listener validates declared exact receipt and receipt-local relation independently."""
import copy
import unittest
from dispatcher.runtime_control import RuntimeControl
from .contract_fixture import ContractFixture
from .receipt_events import history,normal_end
from .state_observation import observe,assert_preserved

class HistoryReceiptContractTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close);self.f.completion();self.f.active('turn-a');self.f.propose();self.f.send(expected='turn-a')
        self.content=copy.deepcopy(self.f.batch()['input']);self.event=history(self.f.root,'client-generic','turn-a','item-receipt',self.content)
    def confirm(self,event=None):
        event=self.event if event is None else event
        state_before=observe(self.f)
        self.assertTrue(self.f.control.event(self.f.binding,event),'T7_EXACT_HISTORY_RECEIPT_NOT_CONFIRMED');assert_preserved(self,self.f,state_before);return self.f.view()
    def test_exact_history_confirms_only_matching_existing_input_attempt_and_batch(self):
        self.assertTrue(self.f.control.accept_input(self.f.binding,'unrelated-operator'));before=self.f.view();after=self.confirm()
        self.assertEqual(after['inputs']['unrelated-operator'],before['inputs']['unrelated-operator']);self.assertEqual(after['inputs']['client-generic']['status'],'accepted')
        receipt=after['inputs']['client-generic']['history_receipt'];self.assertEqual({k:v for k,v in receipt.items() if k!='revision'},{'thread_id':self.f.root,'turn_id':'turn-a','item_id':'item-receipt'})
        self.assertGreater(receipt['revision'],before['inputs']['client-generic']['revision']);self.assertLessEqual(receipt['revision'],after['revision'])
        batch=self.f.batch();self.assertEqual(batch['status'],'confirmed');self.assertEqual(batch['input'],self.content);self.assertEqual(len(batch['attempts']),1)
        self.assertEqual(batch['attempts'][0]['receipt'],{'source':'history','thread_id':self.f.root,'turn_id':'turn-a','item_id':'item-receipt'})
        for key in ['main','wait','workers','completions','history_checkpoint']:self.assertEqual(after.get(key),before.get(key))
    def test_native_empty_text_elements_normalization_preserves_original_batch_input(self):
        event=copy.deepcopy(self.event)
        for item in event['input']:item['text_elements']=[]
        self.confirm(event);self.assertEqual(self.f.batch()['input'],self.content)
    def test_receipt_replay_no_change_and_conflicting_identity_cannot_replace_confirmation(self):
        self.confirm();before=self.f.view();self.assertTrue(self.f.control.event(self.f.binding,self.event));self.assertEqual(self.f.view(),before)
        for field,value in [('item_id','other-item'),('turn_id','other-turn'),('thread_id','foreign'),('input',[{'type':'text','text':'changed'}])]:
            event=copy.deepcopy(self.event);event[field]=value;self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
    def test_completed_own_turn_settles_old_a_without_stopping_current_b(self):
        self.f.active('turn-b');self.assertTrue(self.f.control.accept_input(self.f.binding,'operator-b'));before=self.f.view();self.confirm();accepted=self.f.view()
        self.assertEqual(accepted['inputs']['client-generic']['status'],'accepted');self.assertNotIn('turn-a',accepted['main']['completed_turns'])
        event=normal_end(self.f.root,'client-generic','turn-a','item-receipt',self.content)
        state_before=observe(self.f)
        self.assertTrue(self.f.control.event(self.f.binding,event),'T7_MISSED_OWN_STOP_NOT_RECEIPT_LOCALLY_SETTLED');after=self.f.view();receipt=after['inputs']['client-generic'];relation=receipt['history_settlement']
        assert_preserved(self,self.f,state_before)
        self.assertEqual(receipt['status'],'settled');self.assertEqual(relation['kind'],'accepted-input-in-normal-completed-turn');self.assertEqual(relation['evidence_source'],'authoritative-root-history')
        self.assertEqual(relation['admission_revision'],before['inputs']['client-generic']['revision']);self.assertEqual(relation['receipt_revision'],accepted['inputs']['client-generic']['history_receipt']['revision'])
        self.assertLess(relation['admission_revision'],relation['receipt_revision']);self.assertLessEqual(relation['receipt_revision'],relation['revision']);self.assertLessEqual(relation['revision'],after['revision'])
        for key in ['main','wait','workers','completions','history_checkpoint']:self.assertEqual(after.get(key),before.get(key))
        self.assertEqual(after['inputs']['operator-b'],before['inputs']['operator-b']);self.assertTrue(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),after)
    def test_whole_root_full_turn_paging_can_supply_own_end_relation(self):
        self.confirm();state_before=observe(self.f);event=normal_end(self.f.root,'client-generic','turn-a','item-receipt',self.content,source='thread/turns/list');self.assertTrue(self.f.control.event(self.f.binding,event),'T7_FULL_TURN_SCAN_RELATION_REJECTED');self.assertEqual(self.f.view()['inputs']['client-generic']['status'],'settled');assert_preserved(self,self.f,state_before)
    def test_recorded_exact_normal_stop_settles_history_without_fabricated_relation(self):
        self.f.stop('turn-a');before=self.f.view();after=self.confirm();self.assertEqual(after['inputs']['client-generic']['status'],'settled');self.assertEqual(after['main'],before['main']);self.assertNotIn('history_settlement',after['inputs']['client-generic'])
    def test_matching_history_corroborates_ack_without_another_attempt_or_input_admission(self):
        self.f.ack(turn='turn-a');before=self.f.view();after=self.confirm();self.assertEqual(after['inputs']['client-generic']['revision'],before['inputs']['client-generic']['revision']);self.assertEqual(len(self.f.batch()['attempts']),1);self.assertEqual(after['main'],before['main'])
    def test_runtime_control_reinstantiation_preserves_history_provenance_clock_and_batch(self):
        self.confirm();before=self.f.view();replacement=RuntimeControl(self.f.state);after=replacement.view(self.f.target,self.f.issue,self.f.binding['launch_id']);self.assertEqual(after,before)
        self.assertTrue(replacement.event(self.f.binding,self.event));self.assertEqual(replacement.view(self.f.target,self.f.issue,self.f.binding['launch_id']),before)
    def test_legacy_operator_without_durable_content_is_not_confirmed_by_guessed_history(self):
        self.assertTrue(self.f.control.accept_input(self.f.binding,'legacy-operator'));before=self.f.view();event=history(self.f.root,'legacy-operator','turn-a','legacy-item',[{'type':'text','text':'unknown legacy text'}]);self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before);self.confirm()
    def test_history_confirmation_cannot_revive_forced_retired_binding(self):
        self.confirm();self.assertEqual(self.f.control.retire(self.f.binding,self.f.view()['revision'],reason='forced'),'retired');before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,self.event));self.assertEqual(self.f.view(),before)


def invalid_history_case(changes):
    def test(self):
        event=copy.deepcopy(self.event)
        for path,value in changes:
            node=event
            for key in path[:-1]:node=node[key]
            node[path[-1]]=value(self.content) if callable(value) else value
        before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before);self.confirm()
    return test

HISTORY_INVALID={
 'foreign_root':[(('thread_id',),'foreign')],
 'wrong_steer_turn':[(('turn_id',),'wrong-turn')],
 'missing_client':[(('client_message_id',),'')],
 'null_client':[(('client_message_id',),None)],
 'empty_item':[(('item_id',),'')],
 'null_turn':[(('turn_id',),None)],
 'foreign_scan_root':[(('scan','thread_id'),'foreign')],
 'foreign_scan_turn':[(('scan','turn_id'),'wrong-turn')],
 'skipped_prefix':[(('scan','from_cursor'),'opaque-prefix')],
 'nonfinal_cursor':[(('scan','final_cursor'),'opaque-next')],
 'partial_scan':[(('scan','complete'),False)],
 'unsupported_direction':[(('scan','sort_direction'),'newest')],
 'changed_whitespace':[(('input',),lambda content:[{**item,'text':item['text']+' '} for item in content])],
 'extra_input_element':[(('input',),lambda content:copy.deepcopy(content)+[{'type':'text','text':'extra'}])],
 'nonempty_text_elements':[(('input',),lambda content:[{**item,'text_elements':[{'byteRange':{'start':0,'end':1}}]} for item in content])],
 'unknown_generic_field':[(('input',),lambda content:[{**item,'invented':'field'} for item in content])]
}
for name,changes in HISTORY_INVALID.items():setattr(HistoryReceiptContractTests,'test_history_rejects_'+name+'_then_valid_receipt_confirms',invalid_history_case(changes))


def invalid_relation_case(changes):
    def test(self):
        self.confirm();before=self.f.view();event=normal_end(self.f.root,'client-generic','turn-a','item-receipt',self.content)
        for path,value in changes:
            node=event
            for key in path[:-1]:node=node[key]
            node[path[-1]]=value
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
        self.assertTrue(self.f.control.event(self.f.binding,normal_end(self.f.root,'client-generic','turn-a','item-receipt',self.content)),'T7_VALID_RECEIPT_RELATION_REJECTED')
    return test

RELATION_INVALID={
 'failed_turn':[(('normal_end','status'),'failed')],
 'interrupted_turn':[(('normal_end','status'),'interrupted')],
 'non_null_error':[(('normal_end','error'),{'message':'generic failure'})],
 'summary_items':[(('normal_end','items_view'),'summary')],
 'foreign_root':[(('thread_id',),'foreign')],
 'other_turn_end':[(('turn_id',),'turn-b')],
 'different_item':[(('normal_end','matching_item','id'),'different')],
 'different_client':[(('normal_end','matching_item','client_id'),'different')],
 'changed_input':[(('normal_end','matching_item','input'),[{'type':'text','text':'changed'}])],
 'unsupported_source':[(('normal_end','source'),'resume')],
 'turn_scan_missing':[(('normal_end','source'),'thread/turns/list')],
 'turn_scan_partial':[(('normal_end','source'),'thread/turns/list'),(('normal_end','turn_scan'),{'thread_id':'root-generic','turn_id':None,'sort_direction':'asc','from_cursor':None,'final_cursor':'nonfinal','complete':False})],
 'turn_scan_wrong_scope':[(('normal_end','source'),'thread/turns/list'),(('normal_end','turn_scan'),{'thread_id':'root-generic','turn_id':'turn-a','sort_direction':'asc','from_cursor':None,'final_cursor':None,'complete':True})]
}
for name,changes in RELATION_INVALID.items():setattr(HistoryReceiptContractTests,'test_relation_rejects_'+name+'_without_lifecycle_authority',invalid_relation_case(changes))
