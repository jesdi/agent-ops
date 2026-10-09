"""Fresh public controller contexts on the same genuine Gateway/backend/remote TUI."""
import asyncio
import copy
import json
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .dispatcher_fixture import DispatcherFixture
from .state_observation import observe,assert_preserved

class ControllerAttachmentFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start()
    def factory(self):
        factory=self.flow.factory();self.assertTrue(callable(factory),'T7_ATTACHMENT_UNAVAILABLE_AFTER_HEALTHY_PUBLIC_SETUP');return factory
    def accepted(self,client):return [r for r in self.flow.records() if r['kind']=='native-accepted' and r['item'].get('clientId')==client]
    def result_records(self,identity):return [r for _,rs in self.flow.result_requests() for r in rs if r['identity']==identity]
    async def batch_confirmed(self,identity):
        return await eventually(lambda:next((b for b in self.flow.view()['deliveries'] if identity in b['completion_ids'] and b['status']=='confirmed'),None),'T7_EXACT_RECOVERY_BATCH_NOT_CONFIRMED')
    async def held_result(self,factory):
        entry=await self.flow.ready_composition(factory);a=await self.flow.command('generic lost ACK work');b=await self.flow.command('generic independent active work');await self.flow.qualify([a,b])
        foreground=await self.flow.terminal('input',text='Generic active A',client_message_id='operator-a');turn=foreground['result']['turn']['id']
        barrier=(await self.flow.control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id'];identity=await self.flow.finish(a)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_RESULT_NOT_FORWARDED_TO_OWN_ACK_BARRIER');packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId'];view=self.flow.view();batch=next(v for v in view['deliveries'] if identity in v['completion_ids'])
        self.assertEqual(batch['status'],'pending');self.assertEqual(batch['attempts'][0]['status'],'sent-unconfirmed');self.assertEqual(view['inputs'][client]['status'],'pending');self.assertEqual(view['inputs'][client]['revision'],batch['attempts'][0]['admission_revision']);self.assertEqual(len(self.accepted(client)),1)
        return entry,barrier,identity,packet,client,turn,b
    async def test_first_launch_fully_exits_then_a_b_share_same_gateway_terminal_backend_and_lock(self):
        factory=self.factory();first=await self.flow.first_launch(factory);bootstrap=copy.deepcopy(self.flow.view()['bootstrap']);await self.flow.start_terminal();gateway=self.flow.gateway;lock=gateway.input_lock;backend=self.flow.backend.pid;terminal=self.flow.supervisor.pid
        a=await self.flow.enter(factory);before=self.flow.view();await self.flow.exit(a)
        # Actual operator progress while no controller is attached proves independent lifetime.
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        response=await self.flow.terminal('input',text='Generic handoff operator input',client_message_id='handoff-operator');new_turn=response['result']['turn']['id']
        self.assertIsNone(self.flow.backend.poll());self.assertIsNone(self.flow.supervisor.poll());self.assertEqual(self.flow.view()['main']['turn_id'],before['main']['turn_id'])
        b=await self.flow.enter(factory);await eventually(lambda:self.flow.view()['main']['turn_id']==new_turn and self.flow.view()['main']['status']=='active','T7_FRESH_B_DID_NOT_ORDER_ACTIVE_RECOVERY')
        self.assertIs(self.flow.gateway,gateway);self.assertIs(self.flow.gateway.input_lock,lock);self.assertEqual(self.flow.backend.pid,backend);self.assertEqual(self.flow.supervisor.pid,terminal);self.assertEqual(self.flow.binding,first);self.assertEqual(self.flow.view()['bootstrap'],bootstrap)
        self.assertEqual(sum(r['kind']=='client-request' and r['payload']['method']=='thread/start' for r in self.flow.records()),1)
        self.assertEqual(len(self.accepted(bootstrap['initial_input']['client_message_id'])),1);self.assertEqual(self.flow.view()['history_checkpoint']['baseline_turns'],before['history_checkpoint']['baseline_turns']);await self.flow.exit(b)
    async def test_context_exit_cancels_missing_ack_tasks_without_killing_terminal_or_resending_on_b(self):
        factory=self.factory();entry,barrier,identity,packet,client,turn,other=await self.held_result(factory);old=copy.deepcopy(next(b for b in self.flow.view()['deliveries'] if identity in b['completion_ids']))
        await self.flow.exit(entry);self.assertIsNone(self.flow.backend.poll());self.assertIsNone(self.flow.supervisor.poll());self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending')
        await self.flow.control('complete-turn',thread_id=self.flow.root)
        operator=await self.flow.terminal('input',text='Generic active B during controller handoff',client_message_id='operator-b');turn_b=operator['result']['turn']['id'];self.assertNotEqual(turn,turn_b)
        state_before=observe(self.flow)
        replacement=await self.flow.enter(factory);batch=await self.batch_confirmed(identity);view=self.flow.view()
        assert_preserved(self,self.flow,state_before)
        self.assertEqual(batch['input'],old['input']);self.assertEqual(batch['completion_ids'],old['completion_ids']);self.assertEqual(len(batch['attempts']),1);self.assertEqual(len(self.result_records(identity)),1);self.assertEqual(len(self.accepted(client)),1)
        self.assertEqual(view['inputs'][client]['status'],'settled');self.assertEqual(view['inputs'][client]['history_receipt']['turn_id'],turn);self.assertEqual(view['inputs'][client]['history_settlement']['turn_id'],turn)
        self.assertEqual(view['main']['turn_id'],turn_b);self.assertEqual(view['main']['status'],'active');self.assertNotIn(turn,view['main']['completed_turns']);self.assertEqual(batch['attempts'][0]['receipt']['source'],'history');await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(replacement);assert_preserved(self,self.flow,state_before)
    async def test_d_uncertain_while_distinct_e_and_operator_progress_then_only_d_history_resolves(self):
        factory=self.factory();entry,barrier,identity_d,packet,client_d,turn,command_e=await self.held_result(factory);await self.flow.exit(entry)
        await self.flow.control('complete-turn',thread_id=self.flow.root);await self.flow.terminal('input',text='Generic active B',client_message_id='operator-b')
        fault=(await self.flow.control('history-fault-arm',method='thread/items/list',thread_id=self.flow.root,mode='missing-client'))['fault_id']
        b=await self.flow.enter(factory);self.assertEqual(self.flow.view()['inputs'][client_d]['status'],'pending')
        await self.flow.terminal('input',text='Independent operator while D unresolved',client_message_id='operator-concurrent')
        identity_e=await self.flow.finish(command_e);batch_e=await self.batch_confirmed(identity_e);self.assertEqual(batch_e['attempts'][0]['method'],'turn/steer');self.assertEqual(self.flow.view()['inputs'][client_d]['status'],'pending');self.assertEqual(len(self.result_records(identity_d)),1)
        conditions=[a for a in self.flow.view()['alerts'] if a.get('kind')=='delivery-uncertain' and a['batch_id']!=batch_e['batch_id']];self.assertEqual(len(conditions),1);self.assertEqual(conditions[0]['status'],'pending')
        before=self.flow.view();await self.flow.control('history-fault-clear',fault_id=fault);batch_d=await self.batch_confirmed(identity_d);after=self.flow.view();self.assertEqual(after['inputs']['operator-concurrent'],before['inputs']['operator-concurrent']);self.assertEqual(batch_d['attempts'][0]['receipt']['source'],'history');self.assertEqual(len(self.accepted(client_d)),1)
        await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(b)
    async def test_actual_control_socket_reconnect_keeps_attachment_open_and_exact_root(self):
        factory=self.factory();entry=await self.flow.ready_composition(factory);before=self.flow.view();closed=asyncio.create_task(entry['handle'].wait_closed());baseline_connections=set((await self.flow.control('connections'))['connection_ids']);controller_names={r['payload']['params']['clientInfo']['name'] for r in self.flow.records() if r['kind']=='client-request' and r['payload']['method']=='initialize' and r['connection_id'] in entry['connections']}-{'own-terminal','own-observation'}
        for connection in entry['connections']:await self.flow.control('disconnect',connection_id=connection)
        await self.flow.terminal('input',text='Generic operator during reconnect',client_message_id='reconnect-operator')
        async def resubscribed():
            records=self.flow.records();names={r['connection_id']:r['payload']['params']['clientInfo']['name'] for r in records if r['kind']=='client-request' and r['payload']['method']=='initialize'}
            requests=[r for r in records if r['kind']=='client-request' and r['connection_id'] not in baseline_connections and names.get(r['connection_id']) in controller_names and r['payload']['method']=='thread/resume' and r['payload']['params']['threadId']==self.flow.root]
            return any(reply['kind']=='server-response' and reply['method']=='thread/resume' and reply['connection_id']==request['connection_id'] and type(reply['payload']['id']) is type(request['payload']['id']) and reply['payload']['id']==request['payload']['id'] and 'error' not in reply['payload'] and reply['payload'].get('result',{}).get('thread',{}).get('id')==self.flow.root for request in requests for reply in records)
        await eventually(resubscribed,'T7_CONTROL_TRANSPORT_NOT_EXACT_ROOT_RESUBSCRIBED');self.assertFalse(closed.done());self.assertIsNone(self.flow.backend.poll());self.assertIsNone(self.flow.supervisor.poll())
        command=await self.flow.command('generic reconnect sentinel');await self.flow.qualify([command]);identity=await self.flow.finish(command);await self.batch_confirmed(identity)
        self.assertEqual(self.flow.view()['binding'],before['binding']);self.assertEqual(self.flow.view()['bootstrap'],before['bootstrap']);self.assertEqual(len(self.accepted(before['bootstrap']['initial_input']['client_message_id'])),1);closed.cancel();await asyncio.gather(closed,return_exceptions=True);await self.flow.exit(entry)
    async def test_listener_process_restart_preserves_exact_pending_delivery_and_live_terminal(self):
        factory=self.factory();entry,barrier,identity,packet,client,turn,other=await self.held_result(factory);before=self.flow.view();await self.flow.restart_listener();after=self.flow.view()
        for key in ['binding','bootstrap','wait','history_checkpoint','completions']:self.assertEqual(after[key],before[key])
        batch=next(b for b in after['deliveries'] if identity in b['completion_ids']);prior=next(b for b in before['deliveries'] if identity in b['completion_ids']);self.assertEqual(batch,prior);self.assertEqual(after['inputs'][client],before['inputs'][client]);self.assertIsNone(self.flow.supervisor.poll())
        await self.flow.control('complete-turn',thread_id=self.flow.root);await self.flow.control('barrier-release',barrier_id=barrier);await self.batch_confirmed(identity);self.assertEqual(len(self.result_records(identity)),1);self.assertEqual(len(self.accepted(client)),1);await self.flow.exit(entry)
    async def test_dispatcher_reinstantiation_retains_same_physical_launch_and_pending_inputs(self):
        factory=self.factory();entry,barrier,identity,packet,client,turn,other=await self.held_result(factory);before=self.flow.view();one=DispatcherFixture(self.flow);task=one.pass_once();two=DispatcherFixture(self.flow);again=two.pass_once();after=self.flow.view()
        self.assertEqual(task.stage,again.stage);self.assertEqual(task.park,'');self.assertEqual(again.park,'');self.assertEqual(one.tab.closed,[]);self.assertEqual(two.tab.closed,[])
        for key in ['binding','bootstrap','wait','history_checkpoint','workers','completions','deliveries','inputs']:self.assertEqual(after[key],before[key])
        self.assertEqual(len(self.result_records(identity)),1);self.assertIsNotNone(self.flow.sessions.runtime_view(self.flow.target,self.flow.issue));await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(entry)
    async def test_waiter_cancellation_alone_does_not_close_attachment_gateway_or_terminal(self):
        factory=self.factory();setup=await self.flow.ready_composition(factory);await self.flow.exit(setup)
        barrier=(await self.flow.control('root-reply-barrier-arm',method='thread/resume',thread_id=self.flow.root))['barrier_id'];entry=await self.flow.enter(factory,ready=False)
        async def hit():return (await self.flow.control('root-reply-barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'OWN_WAITER_RESUME_REPLY_NOT_HELD')
        async def entered_wait(waiter,entered):entered.set();return await waiter()
        closed_entered=asyncio.Event();closing=asyncio.create_task(entered_wait(entry['handle'].wait_closed,closed_entered));await closed_entered.wait();self.assertFalse(closing.done(),'OWN_WAIT_CLOSED_NOT_ENTERED_AND_SUSPENDED');self.flow.phase('waiter-entered-suspended',waiter='wait_closed',pending=not closing.done());closing.cancel();await asyncio.gather(closing,return_exceptions=True);self.assertTrue(closing.cancelled())
        ready_entered=asyncio.Event();ready=asyncio.create_task(entered_wait(entry['handle'].wait_ready,ready_entered));await ready_entered.wait();self.assertFalse(ready.done(),'OWN_WAIT_READY_NOT_ENTERED_AND_SUSPENDED');self.flow.phase('waiter-entered-suspended',waiter='wait_ready',pending=not ready.done());ready.cancel();await asyncio.gather(ready,return_exceptions=True);self.assertTrue(ready.cancelled())
        await self.flow.control('root-reply-barrier-release',barrier_id=barrier);readiness=asyncio.create_task(entry['handle'].wait_ready());self.addAsyncCleanup(self.cancel_owned,readiness);await eventually(readiness.done,'T7_ENTERED_WAITER_CANCELLATION_BLOCKED_ATTACHMENT');self.assertFalse(readiness.cancelled());self.assertIsNone(readiness.exception(),'T7_ENTERED_WAITER_CANCELLATION_CLOSED_ATTACHMENT');await self.flow.ready(entry)
        command=await self.flow.command('generic same attachment after entered waiter cancellations');await self.flow.qualify([command])
        response=await self.flow.terminal('input',text='Generic cancellation sentinel',client_message_id='cancel-waiter-operator');self.assertIn('result',response);self.assertIsNone(self.flow.backend.poll());self.assertIsNone(self.flow.supervisor.poll());self.assertEqual(await entry['handle'].wait_ready(),self.flow.binding)
        identity=await self.flow.finish(command);batch=await self.batch_confirmed(identity);client=batch['attempts'][0]['client_message_id'];self.assertEqual(len(self.accepted(client)),1);self.assertEqual(len(self.result_records(identity)),1);self.assertTrue(any(r['kind']=='client-request' and r['connection_id'] in entry['connections'] and r['payload']['method'] in ['turn/start','turn/steer'] and r['payload']['params'].get('clientUserMessageId')==client for r in self.flow.records()),'T7_CANCELLED_WAITER_NO_SAME_ATTACHMENT_DELIVERY');self.flow.phase('entered-waiter-controller-delivery-confirmed',identity=identity,client_message_id=client,controller_connections=sorted(entry['connections']));await self.flow.exit(entry)
    async def test_context_owner_cancellation_is_quiescent_without_killing_terminal_or_rolling_back_held_input(self):
        factory=self.factory();entry=await self.flow.ready_composition(factory);command=await self.flow.command('generic owner cancellation result');await self.flow.qualify([command]);await self.flow.terminal('input',text='Generic active cancellation target',client_message_id='cancel-owner-operator');await self.flow.exit(entry)
        before_connections=set((await self.flow.control('connections'))['connection_ids']);ready=asyncio.get_running_loop().create_future();never=asyncio.Event()
        async def owner():
            async with factory(self.flow.bound,self.flow.binding,self.flow.arguments,backend_path=self.flow.backend_path,input_lock=self.flow.gateway.input_lock) as handle:
                await handle.wait_ready();ready.set_result(handle);await never.wait()
        task=asyncio.create_task(owner());self.addAsyncCleanup(self.cancel_owned,task);handle=await asyncio.wait_for(ready,8);barrier=(await self.flow.control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=self.flow.root,one_shot=True))['barrier_id'];identity=await self.flow.finish(command)
        async def hit():return (await self.flow.control('barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_OWNER_CANCELLATION_ACK_NOT_HELD');packet,_=await self.flow.result(identity);client=packet['params']['clientUserMessageId'];owned=set((await self.flow.control('connections'))['connection_ids'])-before_connections;task.cancel()
        with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(task,8)
        await asyncio.wait_for(handle.wait_closed(),8);self.assertFalse(owned&set((await self.flow.control('connections'))['connection_ids']));self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending');self.assertIsNone(self.flow.backend.poll());self.assertIsNone(self.flow.supervisor.poll());self.assertEqual(len(self.accepted(client)),1)
        await self.flow.control('complete-turn',thread_id=self.flow.root);replacement=await self.flow.enter(factory);await self.batch_confirmed(identity);self.assertEqual(len(self.result_records(identity)),1);await self.flow.control('barrier-release',barrier_id=barrier);await self.flow.exit(replacement)
    async def cancel_owned(self,task):task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_default_attachment_does_not_read_or_forward_its_prompt(self):
        factory=self.factory();await self.flow.first_launch(factory);await self.flow.start_terminal();original=copy.deepcopy(self.flow.view()['bootstrap']);self.flow.arguments.prompt='MUST NOT BECOME A NEW STAGE PROMPT'
        entry=await self.flow.enter(factory);command=await self.flow.command('generic attachment sentinel');await self.flow.qualify([command]);identity=await self.flow.finish(command);await self.batch_confirmed(identity)
        self.assertEqual(self.flow.view()['bootstrap'],original);self.assertFalse(any(part.get('text')==self.flow.arguments.prompt for r in self.flow.records() if r['kind']=='native-accepted' for part in r['item']['content']));await self.flow.exit(entry)
    async def test_lost_root_reply_and_all_correlation_never_creates_second_root_or_prompt(self):
        factory=self.factory();barrier=(await self.flow.control('root-reply-barrier-arm',method='thread/start'))['barrier_id'];first=await self.flow.enter(factory,mode='first-launch',ready=False)
        async def hit():return (await self.flow.control('root-reply-barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_FIRST_ROOT_REQUEST_NOT_OBSERVED');view=self.flow.view();self.assertEqual(view['bootstrap']['root_status'],'attempted-unconfirmed');self.assertIsNone(view['binding']['conversation_id']);self.assertEqual(view['bootstrap']['root_method'],'thread/start');self.assertGreater(view['bootstrap']['root_attempt_revision'],0)
        await self.flow.exit(first);before=self.flow.view();second=await self.flow.enter(factory,ready=False);waiting=asyncio.create_task(second['handle'].wait_ready())
        # Independent native progress is possible, but its unrelated identity has no binding authority.
        foreign=(await self.flow.native.rpc('thread/start',{}))['result']['thread']['id'];await self.flow.native.rpc('thread/read',{'threadId':foreign});self.assertFalse(waiting.done());await self.flow.exit(second);await asyncio.gather(waiting,return_exceptions=True)
        self.assertEqual(self.flow.view()['bootstrap'],before['bootstrap']);self.assertIsNone(self.flow.view()['binding']['conversation_id']);self.assertFalse(any(r['kind']=='native-accepted' for r in self.flow.records()))
        self.assertEqual(sum(r['kind']=='client-request' and r['payload']['method']=='thread/start' for r in self.flow.records()),2);await self.flow.control('root-reply-barrier-release',barrier_id=barrier)
    async def test_dead_same_launch_attachment_both_modes_cannot_revive_or_forward_then_fresh_prepare_is_distinct(self):
        factory=self.factory();self.assertTrue(self.flow.host.event(self.flow.binding,{'type':'service','status':'dead'}));before=self.flow.view();initial=len(self.flow.records())
        for mode in ['attach','first-launch']:
            entry=None
            try:entry=await self.flow.enter(factory,mode=mode,ready=False);await entry['handle'].wait_ready();self.fail('T7_DEAD_ATTACHMENT_INVENTED_READINESS')
            except (RuntimeError,ValueError,ConnectionError):pass
            finally:
                if entry is not None:
                    try:await self.flow.exit(entry)
                    except (RuntimeError,ValueError,ConnectionError):pass
        self.assertEqual(self.flow.view(),before);self.assertFalse(any(r['kind']=='client-request' and r['payload']['method'] in ['thread/start','thread/resume','turn/start','turn/steer'] for r in self.flow.records()[initial:]))
        fresh=self.flow.host.prepare(self.flow.target,self.flow.issue,self.flow.stage,worktree=str(self.flow.worktree));self.assertNotEqual(fresh['binding']['launch_id'],self.flow.binding['launch_id']);self.assertIsNone(fresh['bootstrap']);self.assertIsNone(self.flow.backend.poll())
