"""Public idle routing, durable batch condition and same-launch terminal death."""
import copy
import unittest
from dispatcher.runtime_control import RuntimeControl
from .contract_fixture import ContractFixture
from .state_observation import observe,assert_preserved

class NativeIdleSelectionTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close);self.f.completion();self.f.active('stale-active-turn');self.f.propose()
    def selection(self,**changes):
        event={'type':'delivery/sent','observed_revision':self.f.view()['revision'],'batch_id':'batch-generic','attempt_id':'idle-attempt','client_message_id':'idle-client','method':'turn/start','thread_id':self.f.root,'expected_turn_id':None,'selection':{'source':'thread/read','thread_id':self.f.root,'status':'idle'}};event.update(changes);return event
    def admitted(self,event=None):
        state_before=observe(self.f)
        self.assertTrue(self.f.control.event(self.f.binding,event or self.selection()),'T7_FRESH_NATIVE_IDLE_SELECTION_NOT_ADMITTED');assert_preserved(self,self.f,state_before);return self.f.view()
    def test_positive_native_idle_routes_start_without_normal_main_stop_or_clock_reset(self):
        before=self.f.view();after=self.admitted();attempt=self.f.batch()['attempts'][0]
        self.assertEqual(attempt['method'],'turn/start');self.assertEqual(attempt['expected_turn_id'],None);self.assertEqual(after['inputs']['idle-client'],{'status':'pending','turn_id':None,'revision':attempt['admission_revision']})
        self.assertEqual(after['main'],before['main']);self.assertEqual(after['wait'],before['wait']);self.assertEqual(after['history_checkpoint'],before['history_checkpoint']);self.assertEqual(after['workers'],before['workers'])
        self.assertEqual(self.f.control.retire(self.f.binding,after['revision'],reason='stopped'),'held')
    def test_old_history_uncertainty_does_not_negate_independent_current_native_idle(self):
        self.f.inventory(certainty='unknown');before=self.f.view();after=self.admitted();self.assertEqual(after['inventory'],'unknown');self.assertEqual(after['completions'],before['completions']);self.assertEqual(after['main'],before['main'])
    def test_idle_selection_never_clears_unrelated_pending_or_accepted_input_barriers(self):
        for status in ['pending','accepted']:
            client='operator-'+status;self.assertTrue(self.f.control.accept_input(self.f.binding,client))
            if status=='accepted':self.f.apply({'type':'input/accepted','client_message_id':client,'turn_id':'other-turn'})
            before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,self.selection()));self.assertEqual(self.f.view(),before)
            if status=='accepted':self.f.active('other-turn');self.f.stop('other-turn')
            else:self.f.apply({'type':'input/rejected','client_message_id':client})
        self.f.active('stale-active-turn-new');self.admitted()
    def test_idle_selection_does_not_bypass_current_task_or_stage_gate(self):
        self.f.signal('blocked');before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,self.selection()));self.assertEqual(self.f.view(),before);self.f.signal();self.admitted()


def invalid_selection_case(field,value):
    def test(self):
        event=self.selection();event['selection'][field]=value;before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before);self.admitted()
    return test
for name,field,value in [('foreign_root','thread_id','foreign'),('cached_resume','source','thread/resume'),('historical_turn','source','thread/turns/list'),('active','status','active'),('notloaded','status','notLoaded'),('system_error','status','systemError')]:setattr(NativeIdleSelectionTests,'test_invalid_'+name+'_selection_holds_then_fresh_idle_admits',invalid_selection_case(field,value))

def stale_selection_test(self):
    event=self.selection();self.assertTrue(self.f.control.accept_input(self.f.binding,'intervening-operator'));self.f.apply({'type':'input/rejected','client_message_id':'intervening-operator'});before=self.f.view();self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before);self.admitted()
NativeIdleSelectionTests.test_intervening_revision_invalidates_preread_selection=stale_selection_test

class UncertaintyConditionTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close);self.f.completion();self.f.active('turn-a');self.f.propose();self.f.send(expected='turn-a')
    def event(self,**changes):
        event={'type':'delivery/uncertain','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','message':'Generic ACK lost; automatic resend held.'};event.update(changes);return event
    def record(self,event=None):self.assertTrue(self.f.control.event(self.f.binding,event or self.event()),'T7_BATCH_UNCERTAINTY_CONDITION_NOT_DURABLE')
    def conditions(self):return [a for a in self.f.view()['alerts'] if a['kind']=='delivery-uncertain' and a['batch_id']=='batch-generic']
    def test_repoll_reword_and_runtime_restart_keep_one_pending_logical_batch_condition(self):
        before=self.f.view();self.record();self.record(self.event(message='A different diagnostic for the same lost ACK.'));self.assertEqual(len(self.conditions()),1);self.assertEqual(self.conditions()[0]['status'],'pending');self.assertTrue(self.conditions()[0]['message'])
        retained=self.f.view();self.f.control=RuntimeControl(self.f.state);self.assertEqual(self.f.view(),retained);self.record();self.assertEqual(len(self.conditions()),1)
        self.assertEqual(self.f.view()['wait'],before['wait']);self.assertEqual(self.f.view()['inputs'],before['inputs']);self.assertEqual(self.f.batch()['status'],'pending')
    def test_ack_resolves_only_its_batch_condition_and_stale_uncertainty_cannot_republish(self):
        self.record();self.f.ack(turn='turn-a');before=self.f.view();self.assertEqual(len(self.conditions()),1);self.assertEqual(self.conditions()[0]['status'],'resolved');self.assertFalse(self.f.control.event(self.f.binding,self.event()));self.assertEqual(self.f.view(),before)
    def test_proven_rejection_resolves_condition_without_confirming_batch_then_safe_attempt_reopens_same_record(self):
        self.record();self.f.active('turn-b');rejection={'type':'delivery/rejected','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','rejection':{'kind':'expected-active-turn','code':-32600,'message':'expected active turn id `turn-a` but found `turn-b`'}}
        self.assertTrue(self.f.control.event(self.f.binding,rejection));self.assertEqual(self.f.batch()['status'],'pending');self.assertEqual(self.conditions()[0]['status'],'resolved')
        self.f.send(attempt='attempt-two',client='client-two',expected='turn-b');self.record(self.event(attempt_id='attempt-two',client_message_id='client-two'));self.assertEqual(len(self.conditions()),1);self.assertEqual(self.conditions()[0]['status'],'pending')
    def test_foreign_or_nonpending_attempt_never_creates_condition(self):
        before=self.f.view()
        for field,value in [('batch_id','foreign'),('attempt_id','foreign'),('client_message_id','foreign'),('message','')]:self.assertFalse(self.f.control.event(self.f.binding,self.event(**{field:value})));self.assertEqual(self.f.view(),before)
        self.record()

class TerminalServiceDeathTests(unittest.TestCase):
    def setUp(self):self.f=ContractFixture();self.addCleanup(self.f.close)
    def test_dead_same_physical_launch_rejects_live_and_unknown_without_revision_change(self):
        self.f.apply({'type':'service','status':'dead'});before=self.f.view()
        for status in ['live','unknown']:self.assertFalse(self.f.control.event(self.f.binding,{'type':'service','status':status}),'T7_DEAD_SAME_LAUNCH_REVIVED');self.assertEqual(self.f.view(),before)
        self.assertTrue(self.f.control.event(self.f.binding,{'type':'service','status':'dead'}));self.assertEqual(self.f.view(),before)
        fresh=self.f.control.prepare(self.f.target,self.f.issue,self.f.stage,conversation_id=self.f.root,worktree=str(self.f.worktree));self.assertNotEqual(fresh['binding']['launch_id'],self.f.binding['launch_id']);self.assertTrue(self.f.control.event(fresh['binding'],{'type':'service','status':'live'}));self.assertEqual(self.f.control.view(self.f.target,self.f.issue,fresh['binding']['launch_id'])['service'],'live')
