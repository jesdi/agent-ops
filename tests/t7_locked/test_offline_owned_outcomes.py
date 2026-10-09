"""Missed all-depth native owned outcomes survive attachment replacement exactly once."""
import copy
import unittest
from .composition_fixture import CompositionFixture
from .external_fixture import eventually
from .recovery_flow_fixture import RecoveryFlow

class OfflineOwnedOutcomesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.flow=CompositionFixture(name=self._testMethodName);self.addAsyncCleanup(self.flow.close);await self.flow.start();self.r=RecoveryFlow(self,self.flow)
    async def test_five_depth_child_and_command_offline_outcomes_recover_exactly_once_without_baseline_reset(self):
        factory=self.r.factory();entry=await self.flow.ready_composition(factory);root_running=await self.flow.command('generic root kept running');await self.flow.qualify([root_running]);root_identity={'kind':'command','thread_id':self.flow.root,'initial_item_id':root_running['item_id']}
        def initialized_wait():
            view=self.flow.view()
            return view['wait'] if view.get('inventory')=='known' and isinstance(view.get('wait'),dict) and any(w['identity']==root_identity and w.get('eligible') and w.get('status')=='running' and w.get('inventory_running') for w in view['workers']) else None
        clock=await eventually(initialized_wait,'T7_ROOT_RUNNING_WAIT_NOT_INITIALIZED_BEFORE_DESCENDANTS');self.assertIsInstance(clock['since'],(int,float));self.assertIn(root_identity,clock['ever_reported']);parent=self.flow.root;children=[];turns={}
        for depth in range(5):
            child=(await self.flow.control('spawn-child',thread_id=parent))['thread_id'];children.append(child);turns[child]=(await self.flow.native.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'generic child '+str(depth)}],'clientUserMessageId':'generic-child-'+str(depth)}))['result']['turn']['id'];parent=child
        command=await self.flow.control('command-start',thread_id=parent,command='generic deepest command');qualified_command=await self.flow.control('command-start',thread_id=children[-2],command='generic deep normal owning stop command')
        await eventually(lambda:set(children).issubset({w['identity']['thread_id'] for w in self.flow.view()['workers']}) and any(w['identity'].get('initial_item_id')==qualified_command['item_id'] for w in self.flow.view()['workers']),'T7_DEEP_OWNED_IDENTITIES_NOT_OBSERVED');before=copy.deepcopy(self.flow.view());await self.flow.exit(entry)
        await self.flow.control('complete-turn',thread_id=parent,status='failed',error={'message':'generic deepest failure','codexErrorInfo':None,'additionalDetails':None},text=' Exact failed child λ\n')
        # Failed child ending is an outcome; its command remains unqualified without
        # its own normal Stop. Ancestor normal completions retain distinct identity.
        for child in reversed(children[:-1]):await self.flow.control('complete-turn',thread_id=child,text='Generic completed ancestor')
        qualification=await self.flow.enter(factory);command_identity={'kind':'command','thread_id':children[-2],'initial_item_id':qualified_command['item_id']}
        qualified=await eventually(lambda:next((w for w in self.flow.view()['workers'] if w['identity']==command_identity and w.get('eligible') and w.get('status')=='running' and w.get('inventory_running') and (w.get('qualifying_stop') or {}).get('thread_id')==children[-2] and (w.get('qualifying_stop') or {}).get('turn_id')==turns[children[-2]] and (w.get('qualifying_stop') or {}).get('status')=='completed'),None),'T7_OFFLINE_DESCENDANT_COMMAND_NOT_QUALIFIED_WHILE_RUNNING')
        self.assertEqual(qualified['qualifying_stop'],{'thread_id':children[-2],'turn_id':turns[children[-2]],'status':'completed','evidence_source':'authoritative-child-history'})
        for child in children:await self.r.confirmed({'kind':'agent','thread_id':child,'turn_id':turns[child]})
        await self.flow.exit(qualification)
        await self.flow.control('command-complete',thread_id=children[-2],item_id=qualified_command['item_id'],status='failed',exit_code=-1,output=None,duration_ms=0)
        replacement=await self.flow.enter(factory);expected={'kind':'agent','thread_id':parent,'turn_id':turns[parent]};record=await eventually(lambda:next((c for c in self.flow.view()['completions'] if c['identity']==expected),None),'T7_OFFLINE_FAILED_DEEP_CHILD_OUTCOME_NOT_RECOVERED');await self.r.confirmed(expected)
        self.assertEqual(record['outcome']['status'],'failed');self.assertEqual(record['outcome']['error']['message'],'generic deepest failure');self.assertEqual(record['outcome']['messages'][0]['text'],' Exact failed child λ\n');self.assertEqual(len(self.r.records_for(expected)),1);self.assertFalse(any(w.get('eligible') and w['identity'].get('initial_item_id')==command['item_id'] for w in self.flow.view()['workers']))
        command_identity={'kind':'command','thread_id':children[-2],'initial_item_id':qualified_command['item_id']};await self.r.confirmed(command_identity);native_result=self.r.records_for(command_identity);self.assertEqual(native_result,[{'identity':command_identity,'outcome':{'status':'failed','exit_code':-1,'aggregated_output':None,'duration_ms':0}}])
        after=self.flow.view();self.assertEqual(after['history_checkpoint']['baseline_turns'],before['history_checkpoint']['baseline_turns']);self.assertEqual(after['history_checkpoint']['baseline_workers'],before['history_checkpoint']['baseline_workers']);self.assertEqual(after['wait']['since'],before['wait']['since']);self.assertEqual(after['wait']['ever_reported'],before['wait']['ever_reported']);await self.flow.exit(replacement)
        again=await self.flow.enter(factory);await self.flow.terminal('input',text='Generic offline recovery sentinel',client_message_id='offline-sentinel');await self.flow.exit(again);self.assertEqual(len(self.r.records_for(expected)),1)
    async def test_readable_absence_does_not_erase_previously_known_worker_or_available_outcome(self):
        factory=self.r.factory();entry,barrier,identity,packet,client,turn,other,before=await self.r.held(factory);await self.flow.exit(entry);fault=(await self.flow.control('history-fault-arm',method='thread/items/list',thread_id=self.flow.root,mode='error'))['fault_id'];replacement=await self.flow.enter(factory);await self.r.condition(identity);await self.r.independent(other,client);after=self.flow.view()
        self.assertTrue(any(c['identity']==identity for c in after['completions']));self.assertTrue(any(w['identity']==identity for w in after['workers']));self.assertEqual(after['wait'],before['wait']);self.assertEqual(after['history_checkpoint']['baseline_workers'],before['history_checkpoint']['baseline_workers']);await self.flow.exit(replacement);await self.flow.control('history-fault-clear',fault_id=fault);await self.flow.control('barrier-release',barrier_id=barrier)
