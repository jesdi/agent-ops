"""Fresh host gate and exact task/ticket/launch acceptance using public state writes."""
import copy
from dataclasses import replace
import unittest
from dispatcher.state import Stage,task_key
from .contract_fixture import ContractFixture,result_input

class DeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.f=ContractFixture();self.addCleanup(self.f.close);self.f.completion()
    def held_then_restored(self,break_gate,restore_gate,phase):
        if phase=='send':self.f.active();self.f.propose()
        break_gate();before=self.f.view()
        event=self.f.proposal() if phase=='proposal' else {'type':'delivery/sent','observed_revision':before['revision'],'batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'main-b'}
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertEqual(self.f.view(),before)
        restore_gate()
        if phase=='proposal':self.f.propose()
        else:self.f.send()
    def test_initial_prepare_gap_holds_results_but_allows_independent_operator(self):
        f=ContractFixture(publish=False);self.addCleanup(f.close);f.completion()
        self.assertTrue(f.control.accept_input(f.binding,'initial-operator'))
        self.assertFalse(f.control.event(f.binding,f.proposal()))
        f.publish_task();f.propose()
    def test_changed_implement_ticket_holds_proposal_and_send(self):
        f=ContractFixture(stage='implement',ticket='1');self.addCleanup(f.close);f.completion()
        f.task_state=replace(f.task_state,ticket_cursor=2);f.publish_task();self.assertFalse(f.control.event(f.binding,f.proposal()))
        f.task_state=replace(f.task_state,ticket_cursor=1);f.publish_task();f.active();f.propose()
        f.task_state=replace(f.task_state,ticket_cursor=2);f.publish_task()
        event={'type':'delivery/sent','observed_revision':f.view()['revision'],'batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','method':'turn/steer','thread_id':f.root,'expected_turn_id':'main-b'}
        self.assertFalse(f.control.event(f.binding,event));self.assertNotIn('client-generic',f.view()['inputs'])
    def test_review_done_to_pr_open_blocks_old_binding_without_physical_closure(self):
        self.f.active();self.f.propose();self.f.task_state=replace(self.f.task_state,stage=Stage.PR_OPEN);self.f.publish_task();self.f.signal('working')
        event={'type':'delivery/sent','observed_revision':self.f.view()['revision'],'batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','method':'turn/steer','thread_id':self.f.root,'expected_turn_id':'main-b'}
        self.assertFalse(self.f.control.event(self.f.binding,event));self.assertFalse(self.f.view()['retired']);self.assertEqual(self.f.view()['service'],'live')
    def test_foreign_task_binding_cannot_assign_or_ack_another_task_result(self):
        self.f.active();self.f.propose();self.f.send();before=self.f.view()
        foreign=self.f.control.prepare(self.f.target,202,'review',conversation_id='foreign-root',worktree=str(self.f.worktree))['binding']
        ack={'type':'delivery/ack','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','thread_id':self.f.root,'turn_id':'main-b'}
        self.assertFalse(self.f.control.event(foreign,ack));self.assertEqual(self.f.view(),before)
    def test_replacement_same_conversation_fences_old_launch_results(self):
        self.f.propose();old=self.f.binding
        fresh=self.f.control.prepare(self.f.target,self.f.issue,'review',conversation_id=self.f.root,worktree=str(self.f.worktree))
        self.assertFalse(self.f.control.event(old,{'type':'delivery/ack','batch_id':'batch-generic','attempt_id':'attempt-generic','client_message_id':'client-generic','thread_id':self.f.root,'turn_id':'main-b'}))
        self.assertEqual(fresh['completions'],[]);self.assertEqual(fresh['deliveries'],[]);self.assertNotEqual(fresh['binding']['launch_id'],old['launch_id'])


def make_gate_case(kind,phase):
    def test(self):
        f=self.f
        original=copy.deepcopy(f.task_state)
        if kind in ['task-missing','task-malformed','task-malformed-record']:
            path=f.state/('task-'+task_key(f.target,f.issue)+'.json')
            original_bytes=path.read_bytes()
            def break_gate():
                if kind=='task-missing':path.unlink()
                else:path.write_text('{' if kind=='task-malformed' else '{}')
            def restore_gate():
                path.write_bytes(original_bytes)
                f.publish_task()
        elif kind=='park':
            def break_gate():f.task_state=replace(f.task_state,park='parked');f.publish_task()
            def restore_gate():f.task_state=original;f.publish_task()
        elif kind=='task-stage':
            def break_gate():f.task_state=replace(f.task_state,stage=Stage.BLOCKED);f.publish_task()
            def restore_gate():f.task_state=original;f.publish_task()
        elif kind=='worktree':
            def break_gate():f.task_state=replace(f.task_state,worktree=str(f.home/'different-task'));f.publish_task()
            def restore_gate():f.task_state=original;f.publish_task()
        elif kind=='signal-missing':
            def break_gate():(f.worktree/'.agent/stage.json').unlink()
            def restore_gate():f.signal()
        elif kind=='signal-malformed':
            def break_gate():(f.worktree/'.agent/stage.json').write_text('{')
            def restore_gate():f.signal()
        elif kind=='signal-stage':
            def break_gate():f.signal(stage='implement')
            def restore_gate():f.signal()
        elif kind=='signal-nonworking':
            def break_gate():f.signal('done')
            def restore_gate():f.signal()
        elif kind=='service-dead':
            def break_gate():f.apply({'type':'service','status':'dead'})
            def restore_gate():f.apply({'type':'service','status':'live'})
        self.held_then_restored(break_gate,restore_gate,phase)
    return test
for _kind in ['task-missing','task-malformed','task-malformed-record','park','task-stage','worktree','signal-missing','signal-malformed','signal-stage','signal-nonworking','service-dead']:
    for _phase in ['proposal','send']:
        setattr(DeliveryGateTests,'test_fresh_'+_kind.replace('-','_')+'_at_'+_phase,make_gate_case(_kind,_phase))
