"""Initial uncertainty reconciles the same durable ordinary prompt, never a new root."""
import asyncio
import copy
import unittest
from dispatcher.runtime_http import BoundClient
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .state_observation import assert_absent,assert_preserved

class InitialRecoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start()
    def factory(self):
        factory=self.flow.factory();self.assertTrue(callable(factory),'T7_ATTACHMENT_UNAVAILABLE_AFTER_HEALTHY_PUBLIC_SETUP');return factory
    async def uncertain_initial(self,factory):
        barrier=(await self.flow.control('root-reply-barrier-arm',method='thread/start'))['barrier_id'];entry=await self.flow.enter(factory,mode='first-launch',ready=False)
        async def hit():return (await self.flow.control('root-reply-barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_INITIAL_ROOT_REPLY_BARRIER_NOT_HIT');root_record=next(r for r in self.flow.records() if r['kind']=='root-reply-barrier-hit' and r['barrier_id']==barrier);connection=root_record['connection_id']
        await self.flow.control('hide-active-receipts',enabled=True);await self.flow.control('notification-drop-arm',connection_id=connection,thread_id=None,method='turn/started',one_shot=True)
        await self.flow.control('scoped-fault-arm',point='after-acceptance-before-ack',method='turn/start',connection_id=connection,mode='drop-reply')
        await self.flow.control('root-reply-barrier-release',barrier_id=barrier)
        accepted=await eventually(lambda:next((r for r in self.flow.records() if r['kind']=='native-accepted'),None),'T7_INITIAL_PROMPT_NEVER_REACHED_NATIVE_ACCEPTANCE')
        view=self.flow.view();client=accepted['item']['clientId'];self.assertEqual(view['bootstrap']['initial_input'],{'client_message_id':client,'input':accepted['item']['content']});self.assertEqual(view['inputs'][client]['status'],'pending');self.assertIsNone(view['inputs'][client]['turn_id'])
        await self.flow.exit(entry);self.flow.root=accepted['thread_id'];return client,accepted['turn_id'],copy.deepcopy(self.flow.view())
    async def recover_status(self,status):
        factory=self.factory();client,turn,before=await self.uncertain_initial(factory);await self.flow.control('complete-turn',thread_id=self.flow.root,status=status,error={'message':'generic native failure','codexErrorInfo':None,'additionalDetails':None} if status=='failed' else None)
        state_before=assert_absent(self,self.flow)
        entry=await self.flow.enter(factory);after=self.flow.view();self.assertEqual(after['binding'],before['binding']);self.assertEqual(after['bootstrap'],before['bootstrap']);self.assertEqual(after['inputs'][client]['history_receipt']['turn_id'],turn)
        assert_preserved(self,self.flow,state_before)
        self.assertEqual(after['inputs'][client]['status'],'settled' if status=='completed' else 'accepted');self.assertEqual(after['main'],before['main']);self.assertNotIn(turn,after['main']['completed_turns'])
        self.assertEqual(sum(r['kind']=='native-accepted' and r['item'].get('clientId')==client for r in self.flow.records()),1);self.assertEqual(sum(r['kind']=='client-request' and r['payload']['method']=='thread/start' for r in self.flow.records()),1);await self.flow.exit(entry)
        assert_preserved(self,self.flow,state_before)
    async def test_lost_initial_reply_and_start_completed_exact_history_releases_same_root_readiness(self):await self.recover_status('completed')
    async def test_lost_initial_reply_failed_exact_history_can_prove_acceptance_without_normal_stop(self):await self.recover_status('failed')
    async def test_lost_initial_reply_interrupted_exact_history_can_prove_acceptance_without_normal_stop(self):await self.recover_status('interrupted')
    async def test_unrelated_or_changed_initial_history_stays_held_until_exact_receipt(self):
        factory=self.factory();client,turn,before=await self.uncertain_initial(factory);await self.flow.control('complete-turn',thread_id=self.flow.root)
        native=next(r for r in self.flow.records() if r['kind']=='native-accepted' and r['item'].get('clientId')==client);baseline=set((await self.flow.control('connections'))['connection_ids'])
        fault=(await self.flow.control('history-fault-arm',method='thread/items/list',thread_id=self.flow.root,mode='mismatched-text'))['fault_id'];entry=await self.flow.enter(factory,ready=False);waiting=asyncio.create_task(entry['handle'].wait_ready())
        async def cleanup_waiter():waiting.cancel();await asyncio.gather(waiting,return_exceptions=True)
        self.addAsyncCleanup(cleanup_waiter)
        def exhausted_changed_scopes():
            records=self.flow.records();names={r['connection_id']:r['payload']['params']['clientInfo']['name'] for r in records if r['kind']=='client-request' and r['payload']['method']=='initialize'};scopes=[]
            for observation in records:
                if observation['kind']!='history-observation-fault' or 'mismatched-text' not in observation['modes'] or observation['connection_id'] in baseline or names.get(observation['connection_id']) in [None,'own-terminal','own-observation'] or observation['params'].get('threadId')!=self.flow.root or observation['params'].get('cursor') is not None or observation['params'].get('turnId') is not None or observation['result'].get('nextCursor','missing') is not None:continue
                item=next((e['item'] for e in observation['result']['data'] if e['turnId']==turn and e['item'].get('clientId')==client and e['item']['id']==native['item']['id']),None)
                if item is None or [p.get('text') for p in item['content']]==[p.get('text') for p in native['item']['content']]:continue
                requests=[r for r in records if r['kind']=='client-request' and r['connection_id']==observation['connection_id'] and r['payload']['method']=='thread/items/list' and r['payload'].get('params',{})==observation['params'] and r['sequence']<observation['sequence']]
                if not requests:continue
                request=max(requests,key=lambda r:r['sequence']);response=next((r for r in records if r['kind']=='server-response' and r['method']=='thread/items/list' and r['connection_id']==request['connection_id'] and type(r['payload']['id']) is type(request['payload']['id']) and r['payload']['id']==request['payload']['id'] and r['payload'].get('result')==observation['result'] and r['sequence']>observation['sequence']),None)
                if response is not None:scopes.append((request,response,item))
            return scopes
        await eventually(lambda:exhausted_changed_scopes(),'OWN_INITIAL_CONTROLLER_EXHAUSTED_CHANGED_SCOPE_NOT_OBSERVED');first=exhausted_changed_scopes()[0]
        # A later exhausted controller scan proves ongoing reconciliation, independently
        # of the first faulty response and the observation connection below.
        later=await eventually(lambda:next((scope for scope in exhausted_changed_scopes() if scope[0]['connection_id']==first[0]['connection_id'] and scope[0]['sequence']>first[1]['sequence']),None),'OWN_INITIAL_CONTROLLER_NO_SUBSEQUENT_RECONCILIATION');self.assertNotEqual(later[0]['payload']['id'],first[0]['payload']['id']);self.assertFalse(waiting.done(),'T7_CHANGED_INITIAL_HISTORY_INVENTED_READINESS');self.assertEqual(self.flow.view()['inputs'][client]['status'],'pending');self.flow.phase('initial-controller-exhausted-changed-scopes',connection=first[0]['connection_id'],initial_item_id=native['item']['id'],first_request=first[0]['payload']['id'],first_response_sequence=first[1]['sequence'],later_request=later[0]['payload']['id'],later_response_sequence=later[1]['sequence'],wait_ready_pending=not waiting.done())
        await self.flow.native.rpc('thread/items/list',{'threadId':self.flow.root,'limit':1});await self.flow.exit(entry);await asyncio.gather(waiting,return_exceptions=True)
        self.assertEqual(self.flow.view()['inputs'][client],before['inputs'][client]);self.assertEqual(self.flow.view()['bootstrap'],before['bootstrap']);self.assertEqual(sum(r['kind']=='native-accepted' for r in self.flow.records()),1)
        await self.flow.control('history-fault-clear',fault_id=fault);healthy=await self.flow.enter(factory);self.assertEqual(self.flow.view()['inputs'][client]['history_receipt']['turn_id'],turn);await self.flow.exit(healthy)
    async def test_late_still_correlated_root_reply_binds_exact_original_operation_without_reissue(self):
        factory=self.factory();barrier=(await self.flow.control('root-reply-barrier-arm',method='thread/start'))['barrier_id'];entry=await self.flow.enter(factory,mode='first-launch',ready=False)
        async def hit():return (await self.flow.control('root-reply-barrier-status',barrier_id=barrier))['hits']>0
        await eventually(hit,'T7_CORRELATED_ROOT_HOLD_NOT_OBSERVED');before=self.flow.view();self.assertIsNone(before['binding']['conversation_id']);self.assertEqual(before['bootstrap']['root_status'],'attempted-unconfirmed')
        await self.flow.control('root-reply-barrier-release',barrier_id=barrier);await self.flow.ready(entry);after=self.flow.view();self.assertEqual(after['bootstrap']['root_operation_id'],before['bootstrap']['root_operation_id']);self.assertEqual(after['bootstrap']['root_attempt_revision'],before['bootstrap']['root_attempt_revision']);self.assertEqual(after['bootstrap']['root_status'],'bound')
        self.assertEqual(sum(r['kind']=='client-request' and r['payload']['method']=='thread/start' for r in self.flow.records()),1);self.assertEqual(sum(r['kind']=='native-accepted' for r in self.flow.records()),1);await self.flow.exit(entry)
    async def test_explicit_prepared_resume_uses_exact_selected_root_even_with_newer_foreign_thread(self):
        factory=self.factory();selected=(await self.flow.native.rpc('thread/start',{}))['result']['thread']['id'];foreign=(await self.flow.native.rpc('thread/start',{}))['result']['thread']['id'];self.assertNotEqual(selected,foreign)
        snapshot=self.flow.host.prepare(self.flow.target,self.flow.issue,self.flow.stage,conversation_id=selected,worktree=str(self.flow.worktree));self.flow.binding=snapshot['binding'];self.assertTrue(self.flow.host.event(self.flow.binding,{'type':'service','status':'live'}));self.flow.bound=BoundClient(self.flow.state,self.flow.target,self.flow.issue,self.flow.binding['launch_id'])
        entry=await self.flow.enter(factory,mode='first-launch');view=self.flow.view();self.assertEqual(view['binding']['conversation_id'],selected);self.assertEqual(view['bootstrap']['root_method'],'thread/resume');self.assertEqual(view['bootstrap']['requested_conversation_id'],selected)
        accepted=[r for r in self.flow.records() if r['kind']=='native-accepted'];self.assertEqual(len(accepted),1);self.assertEqual(accepted[0]['thread_id'],selected);await self.flow.exit(entry)
