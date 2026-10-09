"""Actual dispatcher/Sessions public flow with owned physical terminal boundary."""
import json
import time
import unittest
from dispatcher.state import Stage,save
from .dispatcher_fixture import DispatcherFixture
from .external_fixture import eventually
from .layered_dispatcher_fixture import LayeredDispatcherFixture
from .contract_fixture import result_input

class DispatcherDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=LayeredDispatcherFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.dispatcher=DispatcherFixture(self.flow)
    async def pending_batch(self,running=False):
        command=await self.flow.command();commands=[command]
        if running:commands.append(await self.flow.command('generic running cap sentinel'))
        await self.flow.qualify(commands,now=time.time()-20000 if running else None);identity=await self.flow.finish(command)
        known=self.flow.view()
        self.assertEqual(known['main']['status'],'stopped');self.assertEqual(known['inventory'],'known')
        self.assertTrue(all(receipt['status'] in ['settled','rejected'] for receipt in known['inputs'].values()))
        if running:
            self.assertTrue(any(worker['eligible'] and worker['status']=='running' for worker in known['workers']))
            self.assertGreater(time.time()-known['wait']['since'],self.dispatcher.config.background_wait_seconds)
        completion=await eventually(lambda:next((record for record in self.flow.view()['completions'] if record['identity']==identity),None),'SETUP_NO_AVAILABLE_COMMAND_COMPLETION')
        # Record through the real listener. False is proper missing T6 behavior;
        # no private controller call or snapshot write substitutes for admission.
        proposal={'type':'delivery/proposed','observed_revision':self.flow.view()['revision'],'batch_id':'dispatcher-batch','completion_ids':[identity],'input':result_input([completion])}
        self.assertTrue(self.flow.host.event(self.flow.binding,proposal),'T6_NO_LISTENER_BATCH_FOR_DISPATCHER_FLOW')
        return identity
    async def test_pending_available_delivery_holds_dispatcher_ordinary_park_and_session(self):
        await self.pending_batch();before=self.flow.view();task=self.dispatcher.pass_once()
        self.assertEqual(task.stage,Stage.REVIEW);self.assertEqual(task.park,'');self.assertEqual(self.dispatcher.tab.closed,[])
        self.assertEqual(before['wait'],self.flow.view()['wait']);self.assertFalse(self.flow.view()['retired']);self.assertIsNotNone(self.flow.sessions.runtime_view(self.flow.target,self.flow.issue))
    async def test_background_cap_cannot_retire_pending_delivery(self):
        await self.pending_batch(running=True);view=self.flow.view()
        self.assertEqual(self.flow.host.retire(self.flow.binding,view['revision'],reason='background',now=(view['wait']['since'] if view['wait'] else 0)+999999,cap=1),'held')
        task=self.dispatcher.pass_once();self.assertEqual(task.park,'');self.assertEqual(self.dispatcher.tab.closed,[])
    async def test_loop_cap_wins_pending_results_and_fences_before_physical_close(self):
        await self.pending_batch();self.flow.signal(loop='gate',round=3);task=self.dispatcher.pass_once()
        self.assertEqual(task.stage,Stage.REVIEW);self.assertEqual(task.gate_rounds,3);self.assertEqual(task.park,'parked');self.assertEqual(task.slot,-1);self.assertEqual(task.park_note,'gate loop exceeded its cap of 2 rounds')
        self.assertTrue(self.dispatcher.tab.closed);self.assertTrue(all(snapshot['retired'] for snapshot in self.dispatcher.tab.closed))
    async def test_blocked_help_wins_pending_results_and_fences_before_end(self):
        await self.pending_batch();self.flow.signal('blocked',note='Generic help');task=self.dispatcher.pass_once()
        self.assertNotEqual(task.park,'');self.assertTrue(self.dispatcher.tab.closed);self.assertTrue(all(snapshot['retired'] for snapshot in self.dispatcher.tab.closed))
    async def test_review_done_moves_pr_open_blocks_old_results_without_new_close(self):
        await self.pending_batch();self.flow.signal('done',artifact='https://github.com/fixture/repo/pull/123');task=self.dispatcher.pass_once()
        self.assertEqual(task.stage,Stage.PR_OPEN);self.assertEqual(self.dispatcher.tab.closed,[])
        self.assertFalse(self.flow.host.event(self.flow.binding,{'type':'delivery/sent','observed_revision':self.flow.view()['revision'],'batch_id':'dispatcher-batch','attempt_id':'late-attempt','client_message_id':'late-client','method':'turn/start','thread_id':self.flow.root,'expected_turn_id':None}))
    async def test_dead_service_stays_failed_instead_of_result_restart(self):
        await self.pending_batch();self.flow.supervisor.terminate();self.flow.supervisor.wait(timeout=8);task=self.dispatcher.pass_once()
        self.assertEqual(task.stage,Stage.FAILED);self.assertEqual(task.crashed_stage,'review');self.assertIsNotNone(self.flow.supervisor.poll())

    async def test_ci_gate_wins_pending_results_and_fences_before_end(self):
        await self.pending_batch();self.flow.signal('awaiting-ci',run_id=123);task=self.dispatcher.pass_once()
        self.assertNotEqual(task.park,'');self.assertEqual(task.ci_run_id,123)
        self.assertTrue(self.dispatcher.tab.closed);self.assertTrue(all(snapshot['retired'] for snapshot in self.dispatcher.tab.closed))
