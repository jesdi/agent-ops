"""Optional native provenance is atomic on the existing public ordinary receipt."""
import copy
import inspect
import unittest
from .contract_fixture import ContractFixture
from .receipt_events import history
from .state_observation import observe,assert_preserved

class OrdinaryInputProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close);self.f.active('operator-turn')
        self.content=[{'type':'text','text':' Exact λ\n'},{'type':'text','text':''}]
        self.native={'method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'operator-turn','input':self.content}
    def admit(self,client='operator-text',native=None):
        operation=self.f.control.accept_input
        self.assertIn('native_input',inspect.signature(operation).parameters,'T7_ORDINARY_PROVENANCE_CAPABILITY_UNAVAILABLE')
        return operation(self.f.binding,client,native_input=self.native if native is None else native)
    def test_exact_ordinary_provenance_and_pending_receipt_persist_atomically(self):
        self.assertTrue(self.admit(),'T7_ORDINARY_PROVENANCE_NOT_ATOMICALLY_ADMITTED');view=self.f.view();receipt=view['inputs']['operator-text']
        self.assertEqual(receipt,{'status':'pending','turn_id':None,'revision':view['revision'],'native_input':self.native});before=self.f.view()
        self.assertFalse(self.admit());self.assertEqual(self.f.view(),before)
        replacement=type(self.f.control)(self.f.state);self.assertEqual(replacement.view(self.f.target,self.f.issue,self.f.binding['launch_id']),before)
    def test_completed_text_history_recovers_only_that_exact_operator_receipt(self):
        self.assertTrue(self.admit());self.assertTrue(self.f.control.accept_input(self.f.binding,'other-operator'));before=self.f.view()
        state_before=observe(self.f)
        content=[dict(part,text_elements=[]) for part in self.content];event=history(self.f.root,'operator-text','operator-turn','operator-native-item',content)
        self.assertTrue(self.f.control.event(self.f.binding,event),'T7_ORDINARY_HISTORY_RECEIPT_REJECTED');after=self.f.view()
        self.assertEqual(after['inputs']['operator-text']['status'],'accepted');self.assertEqual(after['inputs']['operator-text']['native_input'],self.native);self.assertEqual(after['inputs']['operator-text']['revision'],before['inputs']['operator-text']['revision'])
        self.assertEqual(after['inputs']['other-operator'],before['inputs']['other-operator']);self.assertEqual(after['main'],before['main'])
        assert_preserved(self,self.f,state_before)
    def test_text_whitespace_order_and_nonempty_spans_do_not_match(self):
        self.assertTrue(self.admit());before=self.f.view()
        variants=[list(reversed(self.content)),self.content+[{'type':'text','text':'extra'}],[{**self.content[0],'text':'Exact λ\n'},self.content[1]],[{**self.content[0],'text_elements':[{'byteRange':{'start':0,'end':1}}]},self.content[1]],[{**self.content[0],'unknown':'field'},self.content[1]]]
        for content in variants:
            self.assertFalse(self.f.control.event(self.f.binding,history(self.f.root,'operator-text','operator-turn','operator-native-item',content)));self.assertEqual(self.f.view(),before)
        self.assertTrue(self.f.control.event(self.f.binding,history(self.f.root,'operator-text','operator-turn','operator-native-item',self.content)),'T7_VALID_ORDINARY_RECEIPT_REJECTED')
    def test_richer_schema_valid_input_retains_admission_and_correlated_ack_access(self):
        richer=[{'type':'text','text':'λ','text_elements':[{'byteRange':{'start':0,'end':2},'placeholder':'generic'}]},{'type':'image','url':'https://example.invalid/generic'}]
        native={**self.native,'input':richer};self.assertTrue(self.admit(native=native),'T7_RICHER_ORDINARY_ACCESS_BLOCKED');before=self.f.view()
        self.assertTrue(self.f.control.event(self.f.binding,{'type':'input/accepted','client_message_id':'operator-text','turn_id':'operator-turn'}));after=self.f.view();self.assertEqual(after['inputs']['operator-text']['native_input'],native);self.assertEqual(after['inputs']['operator-text']['revision'],before['inputs']['operator-text']['revision']);self.assertEqual(after['inputs']['operator-text']['status'],'accepted')
        self.assertFalse(self.f.control.event(self.f.binding,history(self.f.root,'operator-text','operator-turn','operator-native-item',richer)));self.assertEqual(self.f.view(),after)
    def test_two_argument_and_null_provenance_access_remain_ordinary_and_cannot_upgrade(self):
        self.assertTrue(self.f.control.accept_input(self.f.binding,'legacy-two-arguments'));before=self.f.view();self.assertNotIn('native_input',before['inputs']['legacy-two-arguments'])
        self.assertFalse(self.admit(client='legacy-two-arguments'));self.assertEqual(self.f.view(),before)
        self.assertTrue(self.f.control.accept_input(self.f.binding,'null-provenance',native_input=None));self.assertNotIn('native_input',self.f.view()['inputs']['null-provenance'])
        self.assertTrue(self.f.control.event(self.f.binding,{'type':'input/accepted','client_message_id':'legacy-two-arguments','turn_id':'operator-turn'}));self.assertEqual(self.f.view()['inputs']['legacy-two-arguments']['status'],'accepted')
    def test_start_provenance_has_null_precondition_and_learns_matching_turn(self):
        native={**self.native,'method':'turn/start','expected_turn_id':None};self.assertTrue(self.admit(native=native));before=self.f.view()
        self.assertTrue(self.f.control.event(self.f.binding,history(self.f.root,'operator-text','accepted-older-turn','native-start-item',self.content)));after=self.f.view();self.assertEqual(after['inputs']['operator-text']['turn_id'],'accepted-older-turn');self.assertEqual(after['main'],before['main'])
    def test_schema_valid_empty_input_is_stored_but_cannot_invent_user_message_receipt(self):
        native={**self.native,'input':[]};self.assertTrue(self.admit(native=native));before=self.f.view();self.assertEqual(before['inputs']['operator-text']['native_input'],native)
        self.assertFalse(self.f.control.event(self.f.binding,history(self.f.root,'operator-text','operator-turn','empty-item',[])));self.assertEqual(self.f.view(),before)
        self.assertTrue(self.f.control.event(self.f.binding,{'type':'input/accepted','client_message_id':'operator-text','turn_id':'operator-turn'}));self.assertEqual(self.f.view()['inputs']['operator-text']['status'],'accepted')
    def test_new_codex_provenance_keeps_ordinary_claude_two_argument_admission_access(self):
        self.assertTrue(self.admit());snapshot=self.f.control.prepare(self.f.target,self.f.issue+1,self.f.stage,runtime='claude',conversation_id='claude-generic',worktree=str(self.f.worktree));binding=snapshot['binding'];self.assertTrue(self.f.control.event(binding,{'type':'service','status':'live'}));self.assertTrue(self.f.control.accept_input(binding,'claude-ordinary'))
        self.assertTrue(self.f.control.event(binding,{'type':'input/accepted','client_message_id':'claude-ordinary','turn_id':'claude-turn'}));view=self.f.control.view(self.f.target,self.f.issue+1,binding['launch_id']);self.assertEqual(view['inputs']['claude-ordinary']['status'],'accepted');self.assertNotIn('native_input',view['inputs']['claude-ordinary'])


def invalid_provenance_case(field,value):
    def test(self):
        self.assertIn('native_input',inspect.signature(self.f.control.accept_input).parameters,'T7_ORDINARY_PROVENANCE_CAPABILITY_UNAVAILABLE')
        native=copy.deepcopy(self.native);native[field]=value;before=self.f.view();self.assertFalse(self.f.control.accept_input(self.f.binding,'invalid',native_input=native));self.assertEqual(self.f.view(),before);self.assertTrue(self.admit(),'T7_VALID_ORDINARY_ADMISSION_REJECTED')
    return test
for name,field,value in [('foreign_root','thread_id','foreign'),('malformed_user_input','input',[{'type':'text','text':False}]),('start_nonnull_precondition','method','turn/start'),('steer_missing_precondition','expected_turn_id',None),('unknown_method','method','new-method')]:setattr(OrdinaryInputProvenanceTests,'test_invalid_'+name+'_never_reserves_partial_input',invalid_provenance_case(field,value))
