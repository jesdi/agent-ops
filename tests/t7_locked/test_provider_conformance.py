"""Own fixture conformance only: no product invocation or acceptance claim."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from websockets.asyncio.client import unix_connect
from .native_client import Client
from .schema_check import Schemas,SchemaError,KEYWORDS

BASE=Path(__file__).resolve().parent
SCHEMAS=BASE/'schemas'
from .artifact_paths import ROOT as ARTIFACTS

async def eventually(predicate):
    deadline=time.monotonic()+5
    while not await predicate():
        assert time.monotonic()<deadline,'FIXTURE_CONFORMANCE_BARRIER_TIMEOUT'
        await asyncio.sleep(.01)

async def exercise(run):
    checks=[]
    sockets=[]
    controller=await unix_connect(str(run/'control.sock'))
    async def control(op,**fields):
        await controller.send(json.dumps({'op':op,**fields}))
        result=json.loads(await controller.recv())
        assert 'fixture_error' not in result,result
        return result['result']
    async def barrier(point,method,thread_id=None):
        return (await control('barrier-arm',point=point,method=method,thread_id=thread_id))['barrier_id']
    async def hit(key):
        async def observed():
            return (await control('barrier-status',barrier_id=key))['hits']>0
        await eventually(observed)
    async def connect():
        client=await Client().open(run/'native.sock');sockets.append(client)
        return client
    try:
        root_client=await connect()
        initialized=await root_client.rpc('initialize',{'clientInfo':{'name':'fixture-controller','version':'1'},'capabilities':{'experimentalApi':True}})
        assert initialized['result']['codexHome']==str(run/'codex-home')
        await root_client.socket.send(json.dumps({'method':'initialized'}))
        root=(await root_client.rpc('thread/start',{'cwd':str(run/'workspace')}))['result']['thread']['id']
        foreign=(await root_client.rpc('thread/start',{}))['result']['thread']['id']
        first=(await root_client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic main work'}],'clientUserMessageId':'bootstrap-generic'}))['result']['turn']['id']
        command=await control('command-start',thread_id=root,command='generic worker')
        command2=await control('command-start',thread_id=root,command='generic unavailable output worker')
        await control('complete-turn',thread_id=root)
        inventory=(await root_client.rpc('thread/backgroundTerminals/list',{'threadId':root,'limit':1}))['result']
        assert inventory['data'][0]['itemId']==command['item_id']
        assert inventory['nextCursor'] is not None
        final_inventory=(await root_client.rpc('thread/backgroundTerminals/list',{'threadId':root,'limit':1,'cursor':inventory['nextCursor']}))['result']
        assert final_inventory['data'][0]['itemId']==command2['item_id'] and final_inventory['nextCursor'] is None
        checks.append('normal lifecycle and exact-owner command survives completed turn')
        await control('command-complete',thread_id=root,item_id=command['item_id'],status='failed',exit_code=7,output='',duration_ms=0)
        await control('command-complete',thread_id=root,item_id=command2['item_id'],status='failed',exit_code=-1,output=None,duration_ms=None)
        history=(await root_client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']['turns']
        outcome=next(item for item in history[0]['items'] if item['id']==command['item_id'])
        assert (outcome['exitCode'],outcome['aggregatedOutput'],outcome['durationMs'],outcome['status'])==(7,'',0,'failed')
        unavailable=next(item for item in history[0]['items'] if item['id']==command2['item_id'])
        assert unavailable['exitCode']==-1 and unavailable['aggregatedOutput'] is None and unavailable['durationMs'] is None
        checks.append('failed commands preserve empty/zero and unavailable null values; terminal pages exhaust')
        active=(await root_client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic foreground'}],'clientUserMessageId':'operator-generic'}))['result']['turn']['id']
        mismatch=await root_client.rpc('turn/steer',{'threadId':root,'expectedTurnId':first,'input':[{'type':'text','text':'must not appear'}],'clientUserMessageId':'stale-generic'})
        assert mismatch['error']=={'code':-32600,'message':f'expected active turn id `{first}` but found `{active}`'}
        full=(await root_client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']['turns']
        assert not any(item.get('clientId')=='stale-generic' for turn in full for item in turn['items'])
        assert len(full)==2
        checks.append('exact obsolete-turn rejection occurs before acceptance and turn creation')
        for _ in range(2):
            ack=await root_client.rpc('turn/steer',{'threadId':root,'expectedTurnId':active,'input':[{'type':'text','text':'same generic payload'}],'clientUserMessageId':'repeat-generic'})
            assert ack['result']=={'turnId':active}
        full=(await root_client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']['turns']
        repeated=[item for turn in full for item in turn['items'] if item.get('clientId')=='repeat-generic']
        assert len(repeated)==2 and repeated[0]['id']!=repeated[1]['id']
        checks.append('native client ID reuse creates independent items')
        parent=root
        descendants=[]
        for depth in range(1,4):
            child=(await control('spawn-child',thread_id=parent))['thread_id']
            descendants.append(child)
            read=(await root_client.rpc('thread/resume',{'threadId':child}))['result']['thread']
            assert read['source']['subAgent']['thread_spawn']=={'depth':depth,'parent_thread_id':parent}
            await root_client.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'generic child work'}]})
            parent=child
        orphan=(await control('spawn-child',thread_id=foreign))['thread_id']
        await control('command-start',thread_id=descendants[-1])
        await control('complete-turn',thread_id=descendants[-1],status='failed',error={'message':'generic child error','codexErrorInfo':None})
        await control('status',thread_id=descendants[0],status={'type':'notLoaded'})
        resumed=(await root_client.rpc('thread/resume',{'threadId':descendants[0]}))['result']['thread']
        assert len(resumed['turns'])==1 and resumed['id']==descendants[0]
        assert (await root_client.rpc('thread/backgroundTerminals/list',{'threadId':root}))['result']['data']==[]
        assert len((await root_client.rpc('thread/backgroundTerminals/list',{'threadId':descendants[-1]}))['result']['data'])==1
        seen=[];cursor=None
        while True:
            page=(await root_client.rpc('thread/list',{'ancestorThreadId':root,'sourceKinds':['subAgentThreadSpawn'],'limit':1,'cursor':cursor,'sortDirection':'asc'}))['result']
            seen.extend(t['id'] for t in page['data']);cursor=page['nextCursor']
            if cursor is None:break
        assert seen==descendants and orphan not in seen
        checks.append('three-depth ancestry and terminals use exact scopes; unloaded parent resume adds no turn')
        await control('complete-turn',thread_id=root)
        async def scan(method,**extra):
            seen=[];cursor=None
            while True:
                page=(await root_client.rpc(method,{'threadId':root,'limit':1,'cursor':cursor,**extra}))['result']
                seen.extend(page['data']);cursor=page['nextCursor']
                if cursor is None:return seen
        items=await scan('thread/items/list',sortDirection='asc')
        reverse=await scan('thread/items/list',sortDirection='desc')
        assert [i['item']['id'] for i in items]==list(reversed([i['item']['id'] for i in reverse]))
        assert len(await scan('thread/turns/list',itemsView='full'))==2
        scoped=await scan('thread/items/list',turnId=first)
        assert all(e['turnId']==first for e in scoped)
        p=(await root_client.rpc('thread/items/list',{'threadId':root,'limit':1}))['result']
        wrong=await root_client.rpc('thread/items/list',{'threadId':foreign,'cursor':p['nextCursor']})
        assert 'error' in wrong
        backwards=(await root_client.rpc('thread/items/list',{'threadId':root,'limit':1,'sortDirection':'desc','cursor':p['backwardsCursor']}))['result']
        assert backwards['data'][0]['item']['id']==p['data'][0]['item']['id']
        checks.append('opaque items/turns pages, reverse anchor and foreign-scope rejection')
        async def exhausted_idle_history():
            entries=[];cursor=None;page_count=0
            while True:
                page=(await root_client.rpc('thread/items/list',{'threadId':root,'limit':1,'cursor':cursor,'sortDirection':'asc'}))['result']
                assert 'nextCursor' in page
                entries.extend(page['data']);page_count+=1;cursor=page['nextCursor']
                if cursor is None:
                    return {'entries':entries,'page_count':page_count,'final_cursor':None}
        before_idle=(await root_client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        assert before_idle['id']==root and before_idle['status']=={'type':'idle'}
        assert [turn['status'] for turn in before_idle['turns']]==['completed','completed']
        before_idle_history=await exhausted_idle_history()
        idle_input=[{'type':'text','text':'generic idle rejection must not be accepted'}]
        idle_reply=await root_client.rpc('turn/steer',{'threadId':root,'expectedTurnId':first,'input':idle_input,'clientUserMessageId':'idle-rejected-generic'})
        assert idle_reply['error']=={'code':-32600,'message':'no active turn to steer'}
        after_idle_history=await exhausted_idle_history()
        after_idle=(await root_client.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        assert before_idle==after_idle
        assert before_idle_history==after_idle_history
        assert after_idle['id']==root and after_idle['status']=={'type':'idle'}
        assert [turn['id'] for turn in after_idle['turns']]==[first,active]
        for item in [entry['item'] for entry in after_idle_history['entries']]+[item for turn in after_idle['turns'] for item in turn['items']]:
            assert item.get('clientId')!='idle-rejected-generic' and item.get('content')!=idle_input
        records=[json.loads(line) for line in (run/'captures.jsonl').read_text().splitlines()]
        assert not any(record['kind']=='native-accepted' and record['item'].get('clientId')=='idle-rejected-generic' for record in records)
        (run/'idle-rejection-proof.json').write_text(json.dumps({'classification':'fixture conformance only','thread_id':root,'expected_turn_id':first,'client_message_id':'idle-rejected-generic','input':idle_input,'reply':idle_reply,'before':before_idle,'after':after_idle,'before_history':before_idle_history,'after_history':after_idle_history,'no_native_acceptance_record':True,'no_extra_turn':True},indent=2)+'\n')
        checks.append('known exact-root idle steer returns native literal before acceptance; exhausted history/full read and turn IDs unchanged')
        await control('status',thread_id=foreign,status={'type':'notLoaded'})
        unknown_reply=await root_client.rpc('turn/steer',{'threadId':foreign,'expectedTurnId':first,'input':idle_input,'clientUserMessageId':'unknown-idle-generic'})
        assert unknown_reply['error']=={'code':-32600,'message':'fixture no active turn; unclassified nonacceptance'}
        await control('status',thread_id=foreign,status={'type':'idle'})
        checks.append('unloaded owner generic error does not impersonate exercised idle rejection')

        independent=await connect()
        assert (await independent.rpc('thread/read',{'threadId':root}))['result']['thread']['turns']==[]
        await independent.rpc('thread/resume',{'threadId':root})
        operator=await connect()
        await operator.rpc('thread/resume',{'threadId':root})
        key=await barrier('root-read','thread/read',root)
        pending=asyncio.create_task(independent.rpc('thread/read',{'threadId':root,'includeTurns':True}))
        await hit(key)
        assert not pending.done()
        new=(await operator.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic operator input'}],'clientUserMessageId':'operator-race-generic'}))['result']['turn']['id']
        await control('barrier-release',barrier_id=key)
        assert (await pending)['result']['thread']['turns'][-1]['id']==new
        checks.append('root-read barrier allows independent operator input')
        mux_barrier=(await control('barrier-arm',point='after-acceptance-before-ack',method='turn/steer',thread_id=root,client_message_id='mux-delayed-generic',one_shot=True))['barrier_id']
        delayed=asyncio.create_task(independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':new,'input':[{'type':'text','text':'generic delayed batch D'}],'clientUserMessageId':'mux-delayed-generic'}))
        await hit(mux_barrier)
        early=await independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':new,'input':[{'type':'text','text':'generic independent batch E'}],'clientUserMessageId':'mux-early-generic'})
        assert early['result']=={'turnId':new} and not delayed.done()
        live_read=(await independent.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        live_inventory=(await independent.rpc('thread/backgroundTerminals/list',{'threadId':root}))['result']
        live_turns=(await independent.rpc('thread/turns/list',{'threadId':root,'itemsView':'full','limit':99}))['result']
        assert live_read['status']=={'type':'active','activeFlags':[]} and not delayed.done()
        assert live_inventory['nextCursor'] is None and any(turn['id']==new for turn in live_turns['data'])
        operator_ack=await independent.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic same-connection operator input'}],'clientUserMessageId':'mux-operator-generic'})
        assert operator_ack['result']['turn']['id']==new and not delayed.done()
        assert (await control('barrier-status',barrier_id=mux_barrier))['hits']==1
        read_after=(await independent.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        client_ids=[item.get('clientId') for turn in read_after['turns'] for item in turn['items']]
        for client_id in ['mux-delayed-generic','mux-early-generic','mux-operator-generic']:
            assert client_ids.count(client_id)==1
        await control('barrier-release',barrier_id=mux_barrier)
        delayed_ack=await delayed
        assert delayed_ack['result']=={'turnId':new} and early['id']>delayed_ack['id']
        (run/'multiplex-proof.json').write_text(json.dumps({'classification':'fixture conformance only','thread_id':root,'turn_id':new,'same_connection':True,'delayed_client_id':'mux-delayed-generic','early_client_id':'mux-early-generic','delayed_ack':delayed_ack,'early_ack':early,'operator_ack':operator_ack,'barrier_hits':1,'inventory_live_before_delayed_ack':live_inventory,'read_live_before_delayed_ack':live_read,'turns_live_before_delayed_ack':live_turns,'distinct_accepts_each_once':True},indent=2)+'\n')
        checks.append('same-connection targeted one-shot D ACK hold permits E ACK, reads/inventory/turns polling and operator input before D release')

        key=await barrier('before-input-validation','turn/steer',root)
        pending=asyncio.create_task(independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':new,'input':[{'type':'text','text':'raced result'}],'clientUserMessageId':'race-stale-generic'}))
        await hit(key)
        await control('complete-turn',thread_id=root)
        next_turn=(await operator.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'new operator turn'}]}))['result']['turn']['id']
        await control('barrier-release',barrier_id=key)
        assert (await pending)['error']['message']==f'expected active turn id `{new}` but found `{next_turn}`'
        checks.append('before-validation race validates latest active turn before mutation')
        key=await barrier('after-acceptance-before-ack','turn/steer',root)
        pending=asyncio.create_task(independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':next_turn,'input':[{'type':'text','text':'late ack result'}],'clientUserMessageId':'delayed-generic'}))
        await hit(key)
        await control('complete-turn',thread_id=root)
        latest=(await operator.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'latest operator turn'}]}))['result']['turn']['id']
        await control('barrier-release',barrier_id=key)
        assert (await pending)['result']['turnId']==next_turn
        assert latest!=next_turn
        checks.append('after-acceptance barrier preserves delayed old-turn ACK during newer active turn')
        await control('fault-arm',point='before-native-operation',method='turn/steer')
        try:
            await independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':latest,'input':[{'type':'text','text':'preaccept fault'}],'clientUserMessageId':'before-fault-generic'})
            assert False,'expected disconnect'
        except ConnectionError:pass
        independent=await connect()
        await independent.rpc('thread/resume',{'threadId':root})
        await control('fault-arm',point='after-acceptance-before-ack',method='turn/steer')
        try:
            await independent.rpc('turn/steer',{'threadId':root,'expectedTurnId':latest,'input':[{'type':'text','text':'postaccept fault'}],'clientUserMessageId':'after-fault-generic'})
            assert False,'expected disconnect'
        except ConnectionError:pass
        read=(await operator.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        found=[item.get('clientId') for turn in read['turns'] for item in turn['items']]
        assert 'before-fault-generic' not in found and found.count('after-fault-generic')==1
        await control('complete-turn',thread_id=root)
        recovery=await connect()
        await recovery.rpc('thread/resume',{'threadId':root})
        receipts=(await recovery.rpc('thread/items/list',{'threadId':root,'turnId':latest,'limit':99}))['result']['data']
        assert sum(item['item'].get('clientId')=='after-fault-generic' for item in receipts)==1
        checks.append('preaccept loss proves nonacceptance; postaccept ACK loss retains one completed-root receipt')
        victim=await connect()
        await victim.rpc('thread/resume',{'threadId':root})
        cleanup_barrier=(await control('barrier-arm',point='after-acceptance-before-ack',method='turn/start',thread_id=root,client_message_id='disconnect-held-generic',one_shot=True))['barrier_id']
        victim_pending=asyncio.create_task(victim.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'generic disconnected accepted input'}],'clientUserMessageId':'disconnect-held-generic'}))
        await hit(cleanup_barrier)
        ids=(await control('connections'))['connection_ids']
        await control('disconnect',connection_id=ids[-1])
        try:
            await victim_pending
            assert False,'expected disconnected pending ACK'
        except ConnectionError:pass
        await victim.reader
        assert not operator.reader.done()
        cleanup_read=(await operator.rpc('thread/read',{'threadId':root,'includeTurns':True}))['result']['thread']
        assert sum(item.get('clientId')=='disconnect-held-generic' for turn in cleanup_read['turns'] for item in turn['items'])==1
        await control('barrier-release',barrier_id=cleanup_barrier)
        checks.append('independent controller disconnect leaves operator channel live')
        checks.append('disconnect with pending accepted ACK cancels held request handler without duplicating or erasing input')
        foreign_read=(await operator.rpc('thread/read',{'threadId':foreign,'includeTurns':True}))['result']['thread']
        assert foreign_read['turns']==[]
        checks.append('all main receipts stay on requested exact root')
        return checks
    finally:
        for client in sockets:
            await client.close()
        await controller.close()


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_capture(path):
    validator=Schemas(SCHEMAS)
    records=[json.loads(line) for line in path.read_text().splitlines()]
    assert not any(record['kind'] in ['schema-failure','handler-failure'] for record in records)
    cleanups=[record for record in records if record['kind']=='handler-cleanup']
    assert cleanups and all(record['all_handlers_finished'] for record in cleanups)
    assert any(record['pending_cancelled']>0 for record in cleanups)
    outstanding={}
    for record in records:
        kind=record['kind']; payload=record.get('payload')
        if kind=='client-request':
            if 'id' not in payload:
                validator.validate('ClientNotification.json',payload)
            else:
                validator.validate('JSONRPCRequest.json',payload)
                validator.validate('ClientRequest.json',payload)
                outstanding[(record['connection_id'],payload['id'])]=payload['method']
        if kind=='server-response':
            if 'error' in payload:
                validator.validate('JSONRPCError.json',payload)
            else:
                validator.validate('JSONRPCResponse.json',payload)
                method=record['method']
                assert outstanding[(record['connection_id'],payload['id'])]==method
                names={'thread/start':'ThreadStart','thread/resume':'ThreadResume','thread/read':'ThreadRead','thread/list':'ThreadList','thread/items/list':'ThreadItemsList','thread/turns/list':'ThreadTurnsList','thread/backgroundTerminals/list':'ThreadBackgroundTerminalsList','turn/start':'TurnStart','turn/steer':'TurnSteer','initialize':'Initialize'}
                validator.validate(('v1/' if method=='initialize' else 'v2/')+names[method]+'Response.json',payload['result'])
        if kind=='server-notification':
            validator.validate('JSONRPCNotification.json',payload)
            validator.validate('ServerNotification.json',payload)
    # Audit every node/reference in each selected generated document, including
    # unused definitions, so no unsupported keyword is silently ignored by branches.
    audited=0
    def audit(schema,root):
        nonlocal audited
        if isinstance(schema,bool):return
        assert not set(schema)-KEYWORDS, set(schema)-KEYWORDS
        audited+=1
        if '$ref' in schema:
            assert schema['$ref'].startswith('#/')
            ref=root
            for segment in schema['$ref'][2:].split('/'):
                ref=ref[segment.replace('~1','/').replace('~0','~')]
        for key in ['properties','definitions']:
            for child in schema.get(key,{}).values():audit(child,root)
        for key in ['allOf','anyOf','oneOf']:
            for child in schema.get(key,[]):audit(child,root)
        for key in ['items','additionalProperties']:
            if key in schema:audit(schema[key],root)
    for path in SCHEMAS.rglob('*.json'):
        if path.name!='manifest.json':
            schema=json.loads(path.read_text());audit(schema,schema)
    negative=[('v2/TurnSteerParams.json',{'threadId':'generic','input':[]}),
        ('v2/TurnSteerResponse.json',{'turnId':None}),
        ('v2/ThreadBackgroundTerminalsListResponse.json',{'data':[{'command':'generic'}]}),
        ('v2/ThreadReadResponse.json',{'thread':{'id':'generic'}}),
        ('ServerNotification.json',{'method':'turn/completed','params':{'threadId':'generic','turn':{'id':'generic','items':[],'status':'working'}}})]
    for name,payload in negative:
        try:
            validator.validate(name,payload)
            raise AssertionError('negative schema smoke unexpectedly passed')
        except SchemaError:pass
    return {'record_count':len(records),'connection_handler_cleanup_count':len(cleanups),'pending_handler_cleanup_exercised':True,'schema_validation_count':sum(validator.counts.values()),
        'schema_validation_by_document':validator.counts,'audited_schema_nodes':audited,'negative_schema_checks':len(negative),
        'schema_hashes':{name:hash_file(SCHEMAS/name) for name in validator.loaded},
        'validator_scope':'All assertion keywords in loaded original documents; local refs audited; draft-07 format annotations not assertions; not general-purpose JSON Schema.'}

class ProviderConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_provider_native_schema_and_concurrent_race_conformance(self):
        run=Path(tempfile.mkdtemp(prefix='t6p-'));run.chmod(0o700)
        log=(run/'provider.log').open('w')
        process=subprocess.Popen([sys.executable,str(BASE/'provider.py'),'--run-dir',str(run)],cwd=BASE,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env={'PATH':os.environ.get('PATH',''),'HOME':str(run/'home'),'CODEX_HOME':str(run/'codex-home'),'PYTHONDONTWRITEBYTECODE':'1'})
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        (ARTIFACTS/'provider-conformance-process-ledger.json').write_text(json.dumps({'pid':process.pid,'pgid':process.pid,'run_directory':str(run)}))
        checks=[]
        try:
            async def ready():
                assert process.poll() is None,'FIXTURE_PROVIDER_EXITED_BEFORE_READY'
                return (run/'ready.json').exists()
            await eventually(ready);checks=await exercise(run)
            self.assertGreaterEqual(len(checks),16)
        finally:
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            log.close()
            # No directory deletion until the entire exact owned group is absent.
            deadline=time.monotonic()+8
            while True:
                try:os.killpg(process.pid,0)
                except ProcessLookupError:break
                assert time.monotonic()<deadline,('OWNED_CONFORMANCE_GROUP_NOT_GONE',process.pid)
                await asyncio.sleep(.025)
            self.assertEqual(process.returncode,0)
            self.assertFalse((run/'native.sock').exists());self.assertFalse((run/'control.sock').exists())
            proof=verify_capture(run/'captures.jsonl')
            proof.update(check_count=len(checks),checks=checks,all_groups_absent=True,owned_pgid=process.pid,run_directory=str(run),provider_exit_code=process.returncode,fixture_conformance_only=True)
            destination=ARTIFACTS/'provider-conformance';destination.mkdir(exist_ok=True)
            for name in ['captures.jsonl','validation-counts.json','provider.log','idle-rejection-proof.json','multiplex-proof.json']:
                if (run/name).exists():(destination/name).write_bytes((run/name).read_bytes())
            proof['capture_sha256']=hash_file(destination/'captures.jsonl')
            self.assertEqual((run/'provider.log').read_text(),'')
            manifest=json.loads((SCHEMAS/'manifest.json').read_text())
            for name,digest in manifest['files'].items():self.assertEqual(hash_file(SCHEMAS/name),digest)
            shutil.rmtree(run);proof['run_directory_absent']=not run.exists()
            (destination/'proof.json').write_text(json.dumps(proof,indent=2)+'\n')
