"""T7 fixture-only conformance; no product/runtime imports or acceptance claim."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from websockets.asyncio.client import unix_connect
from .native_client import Client
from .schema_check import Schemas
from .test_provider_conformance import verify_capture

BASE=Path(__file__).resolve().parent
from .artifact_paths import ROOT
ARTIFACTS=ROOT/'additional-provider-conformance'

async def eventually(predicate):
    deadline=time.monotonic()+5
    while True:
        value=predicate()
        if hasattr(value,'__await__'):value=await value
        if value:return value
        assert time.monotonic()<deadline,'OWN_FIXTURE_CONDITION_TIMEOUT'
        await asyncio.sleep(.01)


def text_semantics(content):
    if not isinstance(content,list) or not content:return None
    normalized=[]
    for part in content:
        if not isinstance(part,dict) or set(part)-{'type','text','text_elements'} or part.get('type')!='text' or not isinstance(part.get('text'),str) or part.get('text_elements',[])!=[]:return None
        normalized.append({'type':'text','text':part['text'],'text_elements':[]})
    return normalized


async def exercise(run):
    checks=[];clients=[];controllers=[]
    async def control(socket,op,**fields):
        await socket.send(json.dumps({'op':op,**fields}));reply=json.loads(await socket.recv());assert 'fixture_error' not in reply,reply;return reply['result']
    a=await unix_connect(str(run/'control.sock'));b=await unix_connect(str(run/'control.sock'));controllers.extend([a,b])
    async def connect(name):
        before=set((await control(a,'connections'))['connection_ids']);client=await Client().open(run/'native.sock');clients.append(client)
        await client.rpc('initialize',{'clientInfo':{'name':name,'version':'1'},'capabilities':{'experimentalApi':True}})
        await client.socket.send(json.dumps({'method':'initialized'}))
        after=set((await control(a,'connections'))['connection_ids']);assert len(after-before)==1;return client,(after-before).pop()
    def records():return [json.loads(line) for line in (run/'captures.jsonl').read_text().splitlines()]
    async def hits(key):return (await control(a,'root-reply-barrier-status',barrier_id=key))['hits']>0
    async def exhausted(client,thread,turn=None):
        data=[];cursor=None;seen=[]
        while True:
            params={'threadId':thread,'sortDirection':'asc','limit':1,'cursor':cursor}
            if turn is not None:params['turnId']=turn
            page=(await client.rpc('thread/items/list',params))['result'];assert 'nextCursor' in page
            data.extend(page['data']);cursor=page['nextCursor']
            if cursor is None:return data,seen
            assert cursor not in seen;seen.append(cursor)
    try:
        controller,cid=await connect('generic-controller');operator,oid=await connect('generic-terminal')
        assert len((await control(a,'control-connections'))['connection_ids'])==2
        root_request=controller.next_id+1
        await control(a,'notification-drop-arm',method='thread/started',connection_id=cid,one_shot=True)
        key=(await control(a,'root-reply-barrier-arm',method='thread/start',connection_id=cid,request_id=root_request))['barrier_id']
        pending=asyncio.create_task(controller.rpc('thread/start',{'cwd':str(run/'workspace')}))
        await eventually(lambda:hits(key));assert not pending.done()
        foreign=(await operator.rpc('thread/start',{}))['result']['thread']['id']
        assert not any(n['method']=='thread/started' for n in controller.notifications)
        assert not any(r['kind']=='server-response' and r['connection_id']==cid and r['payload']['id']==root_request for r in records())
        await control(b,'root-reply-barrier-release',barrier_id=key);reply=await pending;root=reply['result']['thread']['id'];assert reply['id']==root_request and root!=foreign
        assert sum(r['kind']=='client-request' and r['connection_id']==cid and r['payload']['method']=='thread/start' for r in records())==1
        checks.append('held correlated initial root reply releases one exact request; no identity inferred from notifications/other roots')

        lost,lid=await connect('generic-lost-root')
        await control(a,'notification-drop-arm',method='thread/started',connection_id=lid,one_shot=True)
        await control(a,'scoped-fault-arm',point='after-acceptance-before-ack',method='thread/start',connection_id=lid,mode='disconnect')
        try:await lost.rpc('thread/start',{});assert False,'expected lost root reply'
        except ConnectionError:pass
        assert not any(n['method']=='thread/started' for n in lost.notifications)
        assert sum(r['kind']=='client-request' and r['connection_id']==lid and r['payload']['method']=='thread/start' for r in records())==1
        assert not any(r['kind']=='server-response' and r['connection_id']==lid and r['method']=='thread/start' for r in records())
        assert not operator.reader.done()
        checks.append('connection-specific lost root reply leaves root identity unconfirmed and independent terminal connected')

        await operator.rpc('thread/resume',{'threadId':root})
        initial=[{'type':'text','text':' Generic initial input λ\n'}]
        await control(a,'notification-drop-arm',method='turn/started',thread_id=root,connection_id=cid,one_shot=True)
        await control(a,'scoped-fault-arm',point='after-acceptance-before-ack',method='turn/start',connection_id=cid,client_message_id='initial-generic',thread_id=root,mode='drop-reply')
        dropped=asyncio.create_task(controller.rpc('turn/start',{'threadId':root,'input':initial,'clientUserMessageId':'initial-generic'},timeout=20))
        await eventually(lambda:any(r['kind']=='native-accepted' and r['item'].get('clientId')=='initial-generic' for r in records()))
        assert not dropped.done();active=(await operator.rpc('thread/read',{'threadId':root}))['result']['thread'];assert active['status']['type']=='active'
        for method in ['turn/completed','thread/status/changed']:await control(a,'notification-drop-arm',method=method,thread_id=root,connection_id=cid,one_shot=True)
        await control(b,'complete-turn',thread_id=root,status='completed',text='Generic completed output')
        await controller.close()
        try:await dropped;assert False,'expected ACK waiter disconnect'
        except ConnectionError:pass
        assert not any(n['method'] in ['turn/started','turn/completed','thread/status/changed'] and n['params'].get('threadId')==root for n in controller.notifications)
        recovered,rid=await connect('generic-reconnected-controller');await recovered.rpc('thread/resume',{'threadId':root})
        full=(await recovered.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread'];assert full['id']==root and full['status']['type']=='idle' and len(full['turns'])==1
        turn_a=full['turns'][0];assert turn_a['status']=='completed' and turn_a['error'] is None
        entries,cursors=await exhausted(recovered,root,turn_a['id']);assert cursors
        receipt=next(e for e in entries if e['item'].get('clientId')=='initial-generic')
        assert receipt['turnId']==turn_a['id'] and receipt['item']['id']
        assert receipt['item']['content']==[{'type':'text','text':initial[0]['text'],'text_elements':[]}]
        assert text_semantics(receipt['item']['content'])==text_semantics(initial)
        assert any(i['id']==receipt['item']['id'] and i['clientId']=='initial-generic' for i in turn_a['items'])
        assert sum(r['kind']=='native-accepted' and r['item'].get('clientId')=='initial-generic' for r in records())==1
        assert oid in (await control(a,'connections'))['connection_ids']
        checks.append('dropped accepted initial ACK and missed own Stop retain one completed exact-root paginated receipt/full normal turn on same backend')
        checks.append('native omitted text_elements becomes empty only in completed history; exact whitespace/type/order/content preserved')

        turn_b=(await operator.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'Generic B'}],'clientUserMessageId':'generic-b'}))['result']['turn']['id']
        history=(await recovered.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread'];assert history['status']['type']=='active' and history['turns'][-1]['id']==turn_b and history['turns'][0]['status']=='completed'
        assert receipt['turnId']!=turn_b
        checks.append('old normal completed own turn remains distinct from fresh active turn; no invented main Stop relation')
        for text in ['Generic repeated first','Generic repeated changed']:
            ack=await operator.rpc('turn/steer',{'threadId':root,'expectedTurnId':turn_b,'input':[{'type':'text','text':text}],'clientUserMessageId':'duplicate-generic'});assert ack['result']['turnId']==turn_b
        await control(b,'complete-turn',thread_id=root)
        repeated,_=await exhausted(recovered,root,turn_b);items=[e['item'] for e in repeated if e['item'].get('clientId')=='duplicate-generic'];assert len(items)==2 and items[0]['id']!=items[1]['id'] and items[0]['content']!=items[1]['content']
        checks.append('duplicate client ID submits distinct native items with differing immutable content; no provider deduplication')

        first=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':1,'sortDirection':'asc'}))['result'];cursor=first['nextCursor'];assert cursor
        for params in [{'threadId':foreign,'cursor':cursor},{'threadId':root,'turnId':turn_b,'cursor':cursor}]:
            mismatch=await recovered.rpc('thread/items/list',params);assert mismatch['error']['code']==-32602
        cross=(await recovered.rpc('thread/turns/list',{'threadId':root,'cursor':cursor,'itemsView':'full'}));assert cross['error']['code']==-32602
        checks.append('opaque cursor rejects wrong exact root, turn and query scope without cursor parsing')

        async def fault(mode):return (await control(a,'history-fault-arm',method='thread/items/list',thread_id=root,turn_id=turn_a['id'],mode=mode))['fault_id']
        key=await fault('repeat-cursor');p1=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':1,'sortDirection':'asc'}))['result'];p2=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':1,'cursor':p1['nextCursor'],'sortDirection':'asc'}))['result'];assert p2['nextCursor']==p1['nextCursor'];await control(b,'history-fault-clear',fault_id=key)
        healthy,_=await exhausted(recovered,root,turn_a['id']);assert healthy==entries
        checks.append('schema-valid repeated next cursor exercises nonprogressing scan; fresh null-start scan recovers after clearing fault')
        key=await fault('missing-next-cursor');missing=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id']}))['result'];assert 'nextCursor' not in missing;await control(b,'history-fault-clear',fault_id=key)
        checks.append('official optional cursor omission remains distinct from explicit exhausted null')
        key=await fault('error');error=await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id']});assert error['error']=={'code':-32600,'message':'fixture scoped history unreadable'};await control(b,'history-fault-clear',fault_id=key)
        checks.append('scoped generic history error provides no rejection/acceptance fabrication')
        key=await fault('duplicate-page');p1=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':1}))['result'];p2=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':1,'cursor':p1['nextCursor']}))['result'];assert p2['data']==p1['data'];await control(b,'history-fault-clear',fault_id=key)
        checks.append('duplicate exact item page is an explicit fixture observation fault rather than a new native submission')
        for mode in ['mismatched-text','missing-client','null-client']:
            key=await fault(mode);observed=(await recovered.rpc('thread/items/list',{'threadId':root,'turnId':turn_a['id'],'limit':99}))['result']['data'];candidate=next(e['item'] for e in observed if e['item']['id']==receipt['item']['id'])
            if mode=='mismatched-text':assert text_semantics(candidate['content'])!=text_semantics(initial)
            else:assert candidate.get('clientId') is None
            assert (await recovered.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']['turns'][0]['items']==turn_a['items']
            await control(b,'history-fault-clear',fault_id=key)
        assert text_semantics([{'type':'text','text':initial[0]['text'],'generic_unknown':True}]) is None
        checks.append('changed text/absent-null client IDs and generic unknown fields cannot manufacture matching text receipts; native stored history stays unchanged')

        victim,vid=await connect('generic-held-root-disconnect');request_id=victim.next_id+1
        await control(a,'notification-drop-arm',method='thread/started',connection_id=vid,one_shot=True)
        key=(await control(a,'root-reply-barrier-arm',method='thread/start',connection_id=vid,request_id=request_id))['barrier_id'];blocked=asyncio.create_task(victim.rpc('thread/start',{}));await eventually(lambda:hits(key));await control(b,'disconnect',connection_id=vid)
        try:await blocked;assert False,'expected held root waiter disconnect'
        except ConnectionError:pass
        await control(a,'root-reply-barrier-release',barrier_id=key)
        await eventually(lambda:any(r['kind']=='handler-cleanup' and r['connection_id']==vid and r['pending_cancelled']>0 and r['all_handlers_finished'] for r in records()))
        assert not operator.reader.done() and (await operator.rpc('thread/read',{'threadId':root}))['result']['thread']['id']==root
        checks.append('disconnect cancels held correlated root handler while multiple control/operator connections remain live')
        return checks
    finally:
        for client in clients:await client.close()
        for socket in controllers:await socket.close()


class AdditionalConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_t7_preparation_capabilities(self):
        run=Path(tempfile.mkdtemp(prefix='t7p-'));run.chmod(0o700);log=(run/'provider.log').open('w')
        process=subprocess.Popen([sys.executable,str(BASE/'provider.py'),'--run-dir',str(run)],cwd=BASE,env={'PATH':os.environ.get('PATH',''),'HOME':str(run/'home'),'CODEX_HOME':str(run/'codex-home'),'PYTHONDONTWRITEBYTECODE':'1'},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        ARTIFACTS.mkdir(parents=True,exist_ok=True);(ARTIFACTS/'process-ledger.json').write_text(json.dumps({'pid':process.pid,'pgid':process.pid,'run_directory':str(run)}));checks=[]
        try:
            async def ready():self.assertIsNone(process.poll());return (run/'ready.json').exists()
            await eventually(ready);checks=await exercise(run);self.assertEqual(len(checks),13)
        finally:
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            log.close();deadline=time.monotonic()+8
            while True:
                try:os.killpg(process.pid,0)
                except ProcessLookupError:break
                self.assertLess(time.monotonic(),deadline,'OWNED_T7_PREPARATION_GROUP_NOT_GONE');await asyncio.sleep(.025)
            self.assertEqual(process.returncode,0);self.assertFalse((run/'native.sock').exists());self.assertFalse((run/'control.sock').exists())
            proof=verify_capture(run/'captures.jsonl');records=[json.loads(line) for line in (run/'captures.jsonl').read_text().splitlines()]
            extra=Schemas(BASE/'schemas')
            for record in records:
                if record['kind']=='server-notification-dropped':extra.validate('ServerNotification.json',record['payload']);extra.validate('JSONRPCNotification.json',record['payload'])
            connected={r['connection_id'] for r in records if r['kind']=='control-connected'};disconnected={r['connection_id'] for r in records if r['kind']=='control-disconnected'};self.assertEqual(connected,disconnected);self.assertEqual(len(connected),2)
            for name in ['captures.jsonl','validation-counts.json','provider.log']:(ARTIFACTS/name).write_bytes((run/name).read_bytes())
            self.assertEqual((run/'provider.log').read_text(),'');shutil.rmtree(run)
            proof.update(status='DONE provider preparation only',check_count=len(checks),checks=checks,all_groups_absent=True,owned_pgid=process.pid,run_directory=str(run),run_directory_absent=not run.exists(),control_connections_cleaned=len(connected),dropped_notification_schema_validation_count=sum(extra.counts.values()),capture_sha256=hashlib.sha256((ARTIFACTS/'captures.jsonl').read_bytes()).hexdigest(),product_imports=False,native_binary_invoked=False,external_network_used=False)
            (ARTIFACTS/'proof.json').write_text(json.dumps(proof,indent=2)+'\n')
