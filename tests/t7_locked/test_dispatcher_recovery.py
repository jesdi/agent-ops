"""Real Sessions/run_pass retain recovered receipts/clock and existing forced authority."""
import copy
import time
import unittest
from dispatcher.state import Stage
from .contract_fixture import result_input
from .dispatcher_fixture import DispatcherFixture
from .layered_dispatcher_fixture import LayeredDispatcherFixture
from .receipt_events import history,normal_end
from .state_observation import observe,assert_preserved

class DispatcherRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=LayeredDispatcherFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.dispatcher=DispatcherFixture(self.flow)
    async def receipt(self):
        work=await self.flow.command();running=await self.flow.command('generic known cap work');await self.flow.qualify([work,running],now=time.time()-20000);identity=await self.flow.finish(work);before=self.flow.view();record=next(c for c in before['completions'] if c['identity']==identity);content=result_input([record]);self.assertTrue(self.flow.host.event(self.flow.binding,{'type':'delivery/proposed','observed_revision':before['revision'],'batch_id':'recovery-batch','completion_ids':[identity],'input':content}))
        self.assertTrue(self.flow.host.event(self.flow.binding,{'type':'delivery/sent','observed_revision':self.flow.view()['revision'],'batch_id':'recovery-batch','attempt_id':'recovery-attempt','client_message_id':'recovery-client','method':'turn/start','thread_id':self.flow.root,'expected_turn_id':None}));event=history(self.flow.root,'recovery-client','recovery-own-a','recovery-item',content)
        self.assertTrue(self.flow.host.event(self.flow.binding,event),'T7_REAL_LISTENER_HISTORY_RECOVERY_UNAVAILABLE');return identity,content,copy.deepcopy(self.flow.view())
    async def test_dispatcher_reinstantiation_with_receipt_only_acceptance_retains_clock_and_ordinary_cap_hold(self):
        identity,content,before=await self.receipt();self.assertEqual(before['inputs']['recovery-client']['status'],'accepted');self.assertGreater(time.time()-before['wait']['since'],self.dispatcher.config.background_wait_seconds);self.assertTrue(any(w['eligible'] and w['status']=='running' for w in before['workers']))
        self.assertEqual(self.flow.host.retire(self.flow.binding,before['revision'],reason='background',now=before['wait']['since']+20000,cap=10800),'held');task=self.dispatcher.pass_once();replacement=DispatcherFixture(self.flow);again=replacement.pass_once();after=self.flow.view();self.assertEqual(task.park,'');self.assertEqual(again.park,'');self.assertEqual(self.dispatcher.tab.closed,[]);self.assertEqual(replacement.tab.closed,[])
        for field in ['binding','bootstrap','main','wait','history_checkpoint','workers','completions','deliveries','inputs']:self.assertEqual(after.get(field),before.get(field))
    async def test_history_local_settlement_has_no_automatic_cap_retirement_or_clock_reset(self):
        identity,content,before=await self.receipt();state_before=observe(self.flow);self.assertTrue(self.flow.host.event(self.flow.binding,normal_end(self.flow.root,'recovery-client','recovery-own-a','recovery-item',content)));after=self.flow.view();self.assertEqual(after['inputs']['recovery-client']['status'],'settled');self.assertEqual(after['main'],before['main']);self.assertEqual(after['wait'],before['wait']);self.assertEqual(self.flow.host.retire(self.flow.binding,after['revision'],reason='background',now=after['wait']['since']+20000,cap=10800),'held');task=self.dispatcher.pass_once();self.assertEqual(task.park,'');self.assertEqual(self.dispatcher.tab.closed,[]);assert_preserved(self,self.flow,state_before)
    async def force(self,status,**fields):
        _,_,before=await self.receipt();self.flow.signal(status,**fields);task=self.dispatcher.pass_once();self.assertNotEqual(task.park,'');self.assertTrue(self.dispatcher.tab.closed);self.assertTrue(all(v['retired'] for v in self.dispatcher.tab.closed));return task
    async def test_blocked_help_wins_recovered_receipt_and_fences_before_physical_end(self):await self.force('blocked',note='Generic help')
    async def test_ci_wins_recovered_receipt_and_fences_before_physical_end(self):self.assertEqual((await self.force('awaiting-ci',run_id=123)).ci_run_id,123)
    async def test_loop_cap_wins_recovered_receipt_without_reopening_old_attempt(self):
        task=await self.force('working',loop='gate',round=3);self.assertEqual(task.gate_rounds,3);self.assertEqual(task.park_note,'gate loop exceeded its cap of 2 rounds')
    async def test_review_done_pr_open_gate_prevents_new_result_while_retained_receipt_stays_exact(self):
        _,content,before=await self.receipt();self.flow.signal('done',artifact='https://github.com/fixture/repo/pull/123');task=self.dispatcher.pass_once();self.assertEqual(task.stage,Stage.PR_OPEN);self.assertEqual(self.dispatcher.tab.closed,[]);self.assertEqual(self.flow.view()['inputs']['recovery-client'],before['inputs']['recovery-client']);self.assertEqual(self.flow.view()['deliveries'][0]['input'],content)
    async def test_dead_physical_terminal_keeps_failed_stage_and_does_not_prepare_result_restart(self):
        _,_,before=await self.receipt();self.flow.supervisor.terminate();self.flow.supervisor.wait(timeout=8);task=self.dispatcher.pass_once();self.assertEqual(task.stage,Stage.FAILED);self.assertEqual(task.crashed_stage,'review');self.assertEqual(self.flow.view()['binding']['launch_id'],before['binding']['launch_id']);self.assertIsNotNone(self.flow.supervisor.poll())
