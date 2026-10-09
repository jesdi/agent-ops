"""Own new preparation capabilities checked separately from product acceptance."""
import asyncio
import unittest
from pathlib import Path
from .native_fixture import NativeFixture,wait_for
from .schema_check import Schemas,SchemaError

class ExtendedProviderConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.n=NativeFixture(self._testMethodName);self.addAsyncCleanup(self.n.close);await self.n.start()
    async def test_before_root_acceptance_barrier_allows_independent_operator_progress(self):
        barrier=(await self.n.control('root-reply-barrier-arm',method='thread/start',point='before-native-operation'))['barrier_id']
        pending=asyncio.create_task(self.n.client.rpc('thread/start',{}))
        async def hit():return (await self.n.control('root-reply-barrier-status',barrier_id=barrier))['hits']==1
        await wait_for(hit,'FIXTURE_ROOT_BARRIER_NOT_HIT');independent=await self.n.connect('generic-independent')
        self.assertEqual((await independent.rpc('thread/list',{}))['result']['data'],[])
        root=(await independent.rpc('thread/start',{}))['result']['thread']['id'];self.assertTrue(root);self.assertFalse(pending.done())
        await self.n.control('root-reply-barrier-release',barrier_id=barrier);reply=await pending;self.assertNotEqual(reply['result']['thread']['id'],root)
    async def test_explicit_active_history_visibility_fault_keeps_completed_receipt_exact(self):
        root=(await self.n.client.rpc('thread/start',{}))['result']['thread']['id'];content=[{'type':'text','text':' Exact λ\n'}]
        turn=(await self.n.client.rpc('turn/start',{'threadId':root,'input':content,'clientUserMessageId':'generic-visibility'}))['result']['turn']['id']
        await self.n.control('hide-active-receipts',enabled=True);read=(await self.n.client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread'];self.assertEqual(read['turns'][0]['itemsView'],'summary');self.assertEqual(read['turns'][0]['items'],[])
        items=(await self.n.client.rpc('thread/items/list',{'threadId':root}))['result'];self.assertEqual(items['data'],[]);self.assertIsNone(items['nextCursor'])
        await self.n.control('complete-turn',thread_id=root);completed=(await self.n.client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']['turns'][0];self.assertEqual(completed['id'],turn);self.assertEqual(completed['itemsView'],'full');self.assertEqual(completed['items'][0]['content'],[{'type':'text','text':' Exact λ\n','text_elements':[]}]);self.assertEqual(sum(r['kind']=='native-accepted' for r in self.n.records()),1)
    async def test_explicit_wrong_ack_and_malformed_history_are_marked_after_normal_validation(self):
        root=(await self.n.client.rpc('thread/start',{}))['result']['thread']['id'];turn=(await self.n.client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic'}]}))['result']['turn']['id']
        await self.n.control('reply-fault-arm',method='turn/steer',mode='wrong-turn-success');reply=await self.n.client.rpc('turn/steer',{'threadId':root,'expectedTurnId':turn,'input':[{'type':'text','text':'generic accepted'}],'clientUserMessageId':'generic-wrong-ack'})
        self.assertNotEqual(reply['result']['turnId'],turn);Schemas(Path(__file__).with_name('schemas')).validate('v2/TurnSteerResponse.json',reply['result'])
        await self.n.control('reply-fault-arm',method='thread/items/list',mode='malformed-history');reply=await self.n.client.rpc('thread/items/list',{'threadId':root})
        with self.assertRaises(SchemaError):Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadItemsListResponse.json',reply['result'])
        faults=[r for r in self.n.records() if r['kind']=='intentional-after-acceptance-reply-fault'];self.assertEqual([r['mode'] for r in faults],['wrong-turn-success','malformed-history'])
        healthy=(await self.n.client.rpc('thread/items/list',{'threadId':root}))['result'];Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadItemsListResponse.json',healthy);self.assertEqual(sum(r['kind']=='native-accepted' and r['item'].get('clientId')=='generic-wrong-ack' for r in self.n.records()),1)
    async def test_bounded_opaque_pages_preserve_exact_items_and_persistent_bad_packets_are_explicit(self):
        root=(await self.n.client.rpc('thread/start',{}))['result']['thread']['id'];turn=(await self.n.client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'initial'}],'clientUserMessageId':'page-0'}))['result']['turn']['id']
        for i in range(1,7):await self.n.client.rpc('turn/steer',{'threadId':root,'expectedTurnId':turn,'input':[{'type':'text','text':'item '+str(i)}],'clientUserMessageId':'page-'+str(i)})
        await self.n.control('complete-turn',thread_id=root);await self.n.control('page-size-cap',limit=2);cursor=None;seen=[];tokens=[]
        while True:
            params={'threadId':root,'limit':100,'sortDirection':'asc'}
            if cursor is not None:params['cursor']=cursor
            page=(await self.n.client.rpc('thread/items/list',params))['result'];Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadItemsListResponse.json',page);self.assertLessEqual(len(page['data']),2);seen.extend(page['data']);cursor=page['nextCursor']
            if cursor is None:break
            self.assertNotIn(cursor,tokens);tokens.append(cursor)
        self.assertEqual([e['item']['clientId'] for e in seen],['page-'+str(i) for i in range(7)]);self.assertGreater(len(tokens),1)
        await self.n.control('persistent-malformed-items',enabled=True)
        for _ in range(2):
            bad=(await self.n.client.rpc('thread/items/list',{'threadId':root}))['result']
            with self.assertRaises(SchemaError):Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadItemsListResponse.json',bad)
        await self.n.control('persistent-malformed-items',enabled=False);self.assertEqual(len((await self.n.client.rpc('thread/items/list',{'threadId':root,'limit':100}))['result']['data']),2)
    async def test_failed_and_interrupted_completion_keep_exact_schema_and_receipt_identity(self):
        root=(await self.n.client.rpc('thread/start',{}))['result']['thread']['id']
        for status in ['failed','interrupted']:
            turn=(await self.n.client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic '+status}],'clientUserMessageId':status}))['result']['turn']['id']
            await self.n.control('complete-turn',thread_id=root,status=status,error={'message':'generic failure','codexErrorInfo':None,'additionalDetails':None} if status=='failed' else None)
            read=(await self.n.client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result'];Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadReadResponse.json',read);ended=next(t for t in read['thread']['turns'] if t['id']==turn);self.assertEqual(ended['status'],status);self.assertEqual(ended['items'][0]['clientId'],status)
    async def test_typed_foreign_thread_and_turn_observations_do_not_mutate_native_ownership(self):
        root=(await self.n.client.rpc('thread/start',{}))['result']['thread']['id'];turn=(await self.n.client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic scoped'}],'clientUserMessageId':'generic-scoped'}))['result']['turn']['id'];await self.n.control('complete-turn',thread_id=root)
        foreign=(await self.n.control('history-fault-arm',method='thread/read',thread_id=root,mode='foreign-thread'))['fault_id'];bad=(await self.n.client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result'];Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadReadResponse.json',bad);self.assertNotEqual(bad['thread']['id'],root);await self.n.control('history-fault-clear',fault_id=foreign)
        mismatch=(await self.n.control('history-fault-arm',method='thread/items/list',thread_id=root,mode='mismatched-turn'))['fault_id'];page=(await self.n.client.rpc('thread/items/list',{'threadId':root}))['result'];Schemas(Path(__file__).with_name('schemas')).validate('v2/ThreadItemsListResponse.json',page);self.assertNotEqual(page['data'][0]['turnId'],turn);self.assertEqual(page['data'][0]['item']['clientId'],'generic-scoped');await self.n.control('history-fault-clear',fault_id=mismatch)
        healthy=(await self.n.client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread'];self.assertEqual(healthy['id'],root);self.assertEqual(healthy['turns'][0]['id'],turn);self.assertEqual(sum(r['kind']=='native-accepted' for r in self.n.records()),1)
