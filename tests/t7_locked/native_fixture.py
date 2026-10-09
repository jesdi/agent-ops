"""Provider-only process/client fixture; no product imports."""
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
from websockets.asyncio.client import unix_connect
from .artifact_paths import ROOT
from .native_client import Client

async def wait_for(predicate,message,timeout=8):
    deadline=time.monotonic()+timeout
    while True:
        value=predicate()
        if hasattr(value,'__await__'):value=await value
        if value:return value
        assert time.monotonic()<deadline,message
        await asyncio.sleep(.01)

class NativeFixture:
    def __init__(self,name):
        self.run=Path(tempfile.mkdtemp(prefix='t7n-'));self.run.chmod(0o700);self.name=name+'-'+self.run.name;self.clients=[]
        self.artifacts=ROOT/self.name;self.artifacts.mkdir(parents=True);self.log=(self.run/'provider.log').open('w');self.process=None
    async def start(self):
        self.process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('provider.py')),'--run-dir',str(self.run)],env={'PATH':os.environ.get('PATH',''),'HOME':str(self.run/'home'),'CODEX_HOME':str(self.run/'codex-home'),'PYTHONDONTWRITEBYTECODE':'1'},stdout=self.log,stderr=subprocess.STDOUT,start_new_session=True)
        (self.artifacts/'process-ledger.json').write_text(json.dumps({'pid':self.process.pid,'pgid':self.process.pid,'run_directory':str(self.run)}))
        await wait_for(lambda:self.process.poll() is None and (self.run/'ready.json').exists(),'SETUP_OWN_NATIVE_PROVIDER_NOT_READY')
        self.control_socket=await unix_connect(str(self.run/'control.sock'));self.client=await self.connect('own-generic-conformance');return self
    async def connect(self,name):
        client=await Client().open(self.run/'native.sock');self.clients.append(client)
        reply=await client.rpc('initialize',{'clientInfo':{'name':name,'version':'1'},'capabilities':{'experimentalApi':True}});assert 'result' in reply
        await client.socket.send(json.dumps({'method':'initialized'}));return client
    async def control(self,op,**fields):
        await self.control_socket.send(json.dumps({'op':op,**fields}));response=json.loads(await self.control_socket.recv());assert 'fixture_error' not in response,response;return response['result']
    def records(self):return [json.loads(line) for line in (self.run/'captures.jsonl').read_text().splitlines()]
    async def close(self):
        if hasattr(self,'control_socket'):await self.control_socket.close()
        for client in self.clients:await client.close()
        if self.process is not None:
            self.process.terminate()
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            deadline=time.monotonic()+8
            while True:
                try:os.killpg(self.process.pid,0)
                except ProcessLookupError:break
                assert time.monotonic()<deadline,'OWNED_NATIVE_GROUP_NOT_GONE';await asyncio.sleep(.02)
            assert self.process.returncode==0,'OWN_PROVIDER_NONCLEAN_EXIT'
            assert not (self.run/'native.sock').exists() and not (self.run/'control.sock').exists()
        self.log.close()
        records=self.records();assert not any(r['kind'] in ['schema-failure','handler-failure'] for r in records)
        cleanups=[r for r in records if r['kind']=='handler-cleanup'];assert cleanups and all(r['all_handlers_finished'] for r in cleanups)
        for name in ['captures.jsonl','validation-counts.json','provider.log']:(self.artifacts/name).write_bytes((self.run/name).read_bytes())
        from .packet_audit import audit_capture
        (self.artifacts/'packet-audit.json').write_text(json.dumps(audit_capture(self.artifacts/'captures.jsonl'),indent=2)+'\n')
        assert (self.run/'provider.log').read_text()==''
        run=str(self.run);shutil.rmtree(self.run)
        (self.artifacts/'cleanup.json').write_text(json.dumps({'all_groups_absent':True,'groups':[self.process.pid],'run_directory':run,'run_directory_absent':not self.run.exists(),'native_socket_absent':True,'control_socket_absent':True,'all_native_handlers_finished':True,'capture_sha256':hashlib.sha256((self.artifacts/'captures.jsonl').read_bytes()).hexdigest()},indent=2)+'\n')
