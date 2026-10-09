"""Public durable bootstrap events; capability failures occur in healthy test bodies."""
import copy
import tempfile
import unittest
from pathlib import Path
from dispatcher.runtime_control import RuntimeControl

class BootstrapContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='t7-bootstrap-');self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)/'state';self.control=RuntimeControl(self.state)
        self.snapshot=self.control.prepare('fixture',101,'review',worktree=self.temp.name)
        self.binding=self.snapshot['binding']
        self.assertTrue(self.control.event(self.binding,{'type':'service','status':'live'}),'PUBLIC_SERVICE_SETUP_FAILED')
    def view(self):return self.control.view('fixture',101,self.binding['launch_id'])
    def attempted(self,**changes):
        event={'type':'bootstrap/root-attempted','observed_revision':self.view()['revision'],'root_operation_id':'root-operation-generic','root_method':'thread/start','requested_conversation_id':None,'client_message_id':'initial-client-generic','input':[{'type':'text','text':'  Generic initial λ\n'},{'type':'text','text':''}]};event.update(changes);return event
    def record(self,event=None):
        event=self.attempted() if event is None else event
        self.assertTrue(self.control.event(self.binding,event),'T7_MISSING_DURABLE_ROOT_ATTEMPT');return event
    def bound(self):
        self.record();self.assertTrue(self.control.event(self.binding,{'type':'bound','conversation_id':'root-generic','root_operation_id':'root-operation-generic'}),'T7_CORRELATED_ROOT_NOT_BOUND')
        self.binding=self.view()['binding']
    def sent(self):return {'type':'bootstrap/sent','observed_revision':self.view()['revision'],'root_operation_id':'root-operation-generic','client_message_id':'initial-client-generic','thread_id':'root-generic'}
    def test_new_codex_prepare_has_explicit_unattempted_null(self):
        self.assertIn('bootstrap',self.view(),'T7_NEW_PREPARE_MISSING_UNATTEMPTED_PROVENANCE');self.assertIsNone(self.view()['bootstrap'])
    def test_root_attempt_retains_exact_immutable_initial_input_before_root_forward(self):
        event=self.record();view=self.view();record=view['bootstrap']
        self.assertEqual(record,{'root_operation_id':event['root_operation_id'],'root_method':'thread/start','requested_conversation_id':None,'root_attempt_revision':view['revision'],'root_status':'attempted-unconfirmed','initial_input':{'client_message_id':event['client_message_id'],'input':event['input']}})
        self.assertNotIn(event['client_message_id'],view['inputs'])
    def test_identical_root_attempt_replay_changes_nothing_and_conflicts_cannot_replace_it(self):
        event=self.record();before=self.view();self.assertTrue(self.control.event(self.binding,event));self.assertEqual(self.view(),before)
        for field,value in [('root_operation_id','different'),('client_message_id','different'),('input',[{'type':'text','text':'changed'}]),('root_method','thread/resume'),('requested_conversation_id','foreign')]:
            invalid=copy.deepcopy(event);invalid[field]=value;self.assertFalse(self.control.event(self.binding,invalid));self.assertEqual(self.view(),before)
    def test_stale_revision_and_foreign_root_attempt_are_atomic_then_fresh_succeeds(self):
        before=self.view();event=self.attempted(observed_revision=before['revision']-1);self.assertFalse(self.control.event(self.binding,event));self.assertEqual(self.view(),before)
        foreign=copy.deepcopy(self.binding);foreign['launch_id']='foreign-launch';self.assertFalse(self.control.event(foreign,self.attempted()));self.assertEqual(self.view(),before);self.record()
    def test_attempted_root_requires_exact_operation_owned_binding(self):
        self.record();before=self.view()
        for event in [{'type':'bound','conversation_id':'root-generic'},{'type':'bound','conversation_id':'root-generic','root_operation_id':'foreign-operation'}]:self.assertFalse(self.control.event(self.binding,event));self.assertEqual(self.view(),before)
        self.assertTrue(self.control.event(self.binding,{'type':'bound','conversation_id':'root-generic','root_operation_id':'root-operation-generic'}))
        self.assertEqual(self.view()['bootstrap']['root_status'],'bound');self.assertEqual(self.view()['bootstrap']['initial_input']['input'],self.attempted()['input'])
    def test_initial_resume_names_exact_selected_root(self):
        prepared=self.control.prepare('fixture',101,'review',conversation_id='selected-opaque-root',worktree=self.temp.name);self.binding=prepared['binding'];self.assertTrue(self.control.event(self.binding,{'type':'service','status':'live'}))
        event=self.attempted(root_method='thread/resume',requested_conversation_id='selected-opaque-root');self.record(event);before=self.view()
        self.assertFalse(self.control.event(self.binding,{'type':'bound','conversation_id':'foreign','root_operation_id':'root-operation-generic'}));self.assertEqual(self.view(),before)
        self.assertTrue(self.control.event(self.binding,{'type':'bound','conversation_id':'selected-opaque-root','root_operation_id':'root-operation-generic'}));self.assertEqual(self.view()['binding']['conversation_id'],'selected-opaque-root')
    def test_bootstrap_sent_atomically_reserves_existing_pending_receipt(self):
        self.bound();event=self.sent();self.assertTrue(self.control.event(self.binding,event),'T7_INITIAL_INPUT_NOT_ATOMICALLY_RESERVED');view=self.view()
        self.assertEqual(view['inputs']['initial-client-generic'],{'status':'pending','turn_id':None,'revision':view['revision']})
        self.assertEqual(view['bootstrap']['initial_input']['input'],self.attempted()['input']);before=self.view();self.assertTrue(self.control.event(self.binding,event));self.assertEqual(self.view(),before)
        self.assertFalse(self.control.accept_input(self.binding,'initial-client-generic'));self.assertEqual(self.view(),before)
    def test_stale_or_foreign_bootstrap_send_never_partially_admits_input(self):
        self.bound();before=self.view()
        for field,value in [('observed_revision',before['revision']-1),('thread_id','foreign'),('root_operation_id','foreign'),('client_message_id','foreign')]:
            event=self.sent();event[field]=value;self.assertFalse(self.control.event(self.binding,event));self.assertEqual(self.view(),before)
        self.assertTrue(self.control.event(self.binding,self.sent()),'T7_INITIAL_INPUT_NOT_ATOMICALLY_RESERVED')
    def test_root_unbound_or_unattempted_cannot_send_initial_prompt(self):
        self.record();before=self.view();self.assertFalse(self.control.event(self.binding,self.sent()));self.assertEqual(self.view(),before)
        self.assertTrue(self.control.event(self.binding,{'type':'bound','conversation_id':'root-generic','root_operation_id':'root-operation-generic'}));self.binding=self.view()['binding'];self.assertTrue(self.control.event(self.binding,self.sent()))
    def test_bootstrap_ack_uses_existing_receipt_and_preserves_provenance(self):
        self.bound();self.assertTrue(self.control.event(self.binding,self.sent()),'T7_INITIAL_INPUT_NOT_ATOMICALLY_RESERVED');before=self.view();self.assertTrue(self.control.event(self.binding,{'type':'input/accepted','client_message_id':'initial-client-generic','turn_id':'initial-turn'}))
        after=self.view();self.assertEqual(after['bootstrap'],before['bootstrap']);self.assertEqual(after['inputs']['initial-client-generic']['revision'],before['inputs']['initial-client-generic']['revision']);self.assertEqual(after['inputs']['initial-client-generic']['status'],'accepted')


def invalid_attempt_case(field,value):
    def test(self):
        before=self.view();event=self.attempted(**{field:value});self.assertFalse(self.control.event(self.binding,event));self.assertEqual(self.view(),before);self.record()
    return test
for name,field,value in [('empty_input','input',[]),('null_input','input',None),('missing_text','input',[{'type':'text'}]),('richer_initial_input','input',[{'type':'image','url':'https://example.invalid/generic'}]),('nonempty_initial_spans','input',[{'type':'text','text':'generic','text_elements':[{'byteRange':{'start':0,'end':1}}]}]),('empty_operation','root_operation_id',''),('empty_client','client_message_id',''),('start_named_resume_root','requested_conversation_id','foreign')]:setattr(BootstrapContractTests,'test_invalid_'+name+'_attempt_is_atomic_then_valid_attempt_records',invalid_attempt_case(field,value))
