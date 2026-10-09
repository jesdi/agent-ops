"""Own provider-only proof that duplicate native child evidence never changes input/history."""
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
RPC={'thread/start':'ThreadStart','thread/read':'ThreadRead','thread/resume':'ThreadResume','turn/start':'TurnStart'}

BASE=Path(__file__).resolve().parent
ARTIFACTS=Path(os.environ.get('T6_LOCK_ARTIFACTS','/tmp/t6-lock-evidence'))

class DuplicateChildFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_stored_child_completion_replay_has_no_native_input_or_turn_mutation(self):
        run=Path(tempfile.mkdtemp(prefix='t6dup-'));log=(run/'provider.log').open('w')
        process=subprocess.Popen([sys.executable,os.environ.get('T6_DUPLICATE_PROVIDER',str(BASE/'provider.py')),'--run-dir',str(run),'--schemas',str(BASE/'schemas')],cwd=BASE,env={'PATH':os.environ.get('PATH',''),'HOME':str(run/'home'),'CODEX_HOME':str(run/'codex-home'),'PYTHONDONTWRITEBYTECODE':'1'},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        ARTIFACTS.mkdir(parents=True,exist_ok=True);(ARTIFACTS/'duplicate-child-process-ledger.json').write_text(json.dumps({'pid':process.pid,'pgid':process.pid,'run_directory':str(run)}))
        client=None;control=None;before=None;after=None;proof={}
        try:
            deadline=time.monotonic()+5
            while not (run/'ready.json').exists():
                self.assertIsNone(process.poll());self.assertLess(time.monotonic(),deadline);await asyncio.sleep(.01)
            client=await Client().open(run/'native.sock');control=await unix_connect(str(run/'control.sock'))
            async def operation(op,**fields):
                await control.send(json.dumps({'op':op,**fields}));return json.loads(await control.recv())
            root=(await client.rpc('thread/start',{}))['result']['thread']['id']
            await client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':'Generic root input'}],'clientUserMessageId':'generic-root-client'})
            child=(await operation('spawn-child',thread_id=root))['result']['thread_id'];await client.rpc('thread/resume',{'threadId':child})
            turn=(await client.rpc('turn/start',{'threadId':child,'input':[{'type':'text','text':'Generic child input'}],'clientUserMessageId':'generic-child-client'}))['result']['turn']['id']
            response=await operation('complete-turn',thread_id=child,status='failed',text='Generic child failure output',error={'message':'Generic child failure','codexErrorInfo':None});self.assertTrue(response['result'])
            before=(await client.rpc('thread/read',{'threadId':child,'includeTurns':True}))['result']['thread']
            first=[n for n in client.notifications if n['method']=='turn/completed' and n['params']['threadId']==child]
            self.assertEqual(len(first),1);self.assertEqual(first[0]['params']['turn']['id'],turn)
            captures=lambda:[json.loads(line) for line in (run/'captures.jsonl').read_text().splitlines()]
            accepted_before=[r for r in captures() if r['kind']=='native-accepted']
            duplicate=await operation('duplicate-turn-completion',thread_id=child,turn_id=turn)
            self.assertNotIn('fixture_error',duplicate,'OWN_PROVIDER_MISSING_EXACT_CHILD_COMPLETION_REPLAY')
            self.assertTrue(duplicate['result'])
            after=(await client.rpc('thread/read',{'threadId':child,'includeTurns':True}))['result']['thread']
            completed=[n for n in client.notifications if n['method']=='turn/completed' and n['params']['threadId']==child]
            self.assertEqual(completed,[first[0],first[0]])
            self.assertEqual(before,after);self.assertEqual(len(after['turns']),1)
            self.assertEqual([r for r in captures() if r['kind']=='native-accepted'],accepted_before)
            ordinary=await operation('complete-turn',thread_id=child,status='failed')
            self.assertIn('fixture_error',ordinary);self.assertEqual(ordinary['fixture_error'],"(-32600, 'fixture no active turn')")
            unchanged=(await client.rpc('thread/read',{'threadId':child,'includeTurns':True}))['result']['thread'];self.assertEqual(unchanged,before)
            proof={'fixture_conformance_only':True,'duplicate_payload_identical':True,'no_extra_native_input':True,'no_extra_turn':True,'full_native_history_unchanged':True,'ordinary_complete_turn_still_rejects_inactive':True,'before':before,'after':after,'duplicate_notification':first[0]}
        finally:
            if client is not None:await client.close()
            if control is not None:await control.close()
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            log.close();deadline=time.monotonic()+8
            while True:
                try:os.killpg(process.pid,0)
                except ProcessLookupError:break
                self.assertLess(time.monotonic(),deadline,'OWNED_DUPLICATE_FIXTURE_GROUP_NOT_GONE');await asyncio.sleep(.025)
            self.assertEqual(process.returncode,0);self.assertFalse((run/'native.sock').exists());self.assertFalse((run/'control.sock').exists())
            records=[json.loads(line) for line in (run/'captures.jsonl').read_text().splitlines()]
            self.assertFalse(any(r['kind'] in ['schema-failure','handler-failure'] for r in records))
            validator=Schemas(BASE/'schemas')
            for record in records:
                if record['kind']=='client-request':validator.validate('ClientRequest.json',record['payload']);validator.validate('JSONRPCRequest.json',record['payload'])
                elif record['kind']=='server-response':
                    validator.validate('JSONRPCError.json' if 'error' in record['payload'] else 'JSONRPCResponse.json',record['payload'])
                    if 'result' in record['payload']:validator.validate(('v1/' if record['method']=='initialize' else 'v2/')+RPC[record['method']]+'Response.json',record['payload']['result'])
                elif record['kind']=='server-notification':validator.validate('ServerNotification.json',record['payload']);validator.validate('JSONRPCNotification.json',record['payload'])
            self.assertTrue(all(r['all_handlers_finished'] for r in records if r['kind']=='handler-cleanup'))
            destination=ARTIFACTS/'duplicate-child-conformance';destination.mkdir(exist_ok=True)
            for name in ['captures.jsonl','provider.log','validation-counts.json']:(destination/name).write_bytes((run/name).read_bytes())
            proof.update(owned_group_absent=True,owned_pgid=process.pid,provider_exit_code=process.returncode,schema_validation_count=sum(validator.counts.values()),capture_sha256=hashlib.sha256((destination/'captures.jsonl').read_bytes()).hexdigest(),run_directory=str(run))
            self.assertEqual((run/'provider.log').read_text(),'');shutil.rmtree(run);proof['run_directory_absent']=not run.exists();(destination/'proof.json').write_text(json.dumps(proof,indent=2)+'\n')
