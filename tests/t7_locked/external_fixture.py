"""Own real listener/supervisor/gateway fixture with strict owned-group cleanup."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from websockets.asyncio.client import unix_connect
from dispatcher.runtime_control import RuntimeClient
from dispatcher.sessions import Sessions
from dispatcher.state import Stage,TaskState,save
from .contract_fixture import identity_key
from .native_client import Client

SOURCE=Path(__file__).resolve().parents[2]
HELPERS=Path(__file__).resolve().parent
from .artifact_paths import ROOT as ARTIFACTS

async def eventually(predicate,message,timeout=8):
    deadline=time.monotonic()+timeout
    while True:
        value=predicate()
        if hasattr(value,'__await__'):value=await value
        if value:return value
        if time.monotonic()>deadline:raise AssertionError(message)
        await asyncio.sleep(.025)

class ExternalFixture:
    def __init__(self,name='flow',issue=101,stage='review',ticket='',publish=True,state_dir=None):
        self.directory=Path(tempfile.mkdtemp(prefix='t7x-'))
        self.shared_state=state_dir is not None;self.state=Path(state_dir) if state_dir else self.directory/'state';self.worktree=self.directory/'task';self.run=self.directory/'fake';self.run.mkdir();self.worktree.mkdir();self.directory.chmod(0o700)
        (self.worktree/'.agent').mkdir();self.name=name+'-'+self.directory.name;self.case_name=name;self.issue=issue;self.target='fixture';self.stage=stage;self.ticket=ticket;self.processes=[];self.logs=[];self.clients=[]
        self.env={'PATH':str(self.directory/'bin')+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''),'PYTHONPATH':str(SOURCE),'HOME':str(self.directory/'home'),'CODEX_HOME':str(self.directory/'codex-home'),'XDG_CONFIG_HOME':str(self.directory/'xdg'),'PYTHONDONTWRITEBYTECODE':'1','AGENT_OPS_STATE_DIR':str(self.state),'T6_FIXTURE_RUN_DIR':str(self.run)}
        for name in ['home','codex-home','xdg','bin']:(self.directory/name).mkdir()
        wrapper=self.directory/'bin/codex';wrapper.write_text('#!'+sys.executable+'\nimport runpy,sys\nsys.path.insert(0,'+repr(str(HELPERS))+')\nrunpy.run_path('+repr(str(HELPERS/'codex_fake.py'))+',run_name="__main__")\n');wrapper.chmod(0o700)
        self.task=TaskState(issue=issue,target=self.target,stage=Stage(stage),slot=0,worktree=str(self.worktree),branch='fixture',title='Generic task',updated_at='2026-10-08T00:00:00Z',ticket_cursor=int(ticket or 0),ticket_count=max(int(ticket or 0),1))
        if publish:save(self.state,self.task)
        self.signal()
        self.host=RuntimeClient(self.state)
        self.sessions=Sessions(state_dir=self.state)
    def signal(self,status='working',stage=None,**fields):
        (self.worktree/'.agent/stage.json').write_text(json.dumps({'stage':stage or self.stage,'status':status,**fields}))
    def launch(self,args,label,cwd=None,env=None):
        log=(self.directory/(label+'.log')).open('w');self.logs.append(log)
        process=subprocess.Popen(args,cwd=cwd or SOURCE,env=env or self.env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        self.processes.append((process,process.pid,label))
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        (ARTIFACTS/(self.name+'-process-ledger.json')).write_text(json.dumps({'run_directory':str(self.directory),'processes':[{'pid':p.pid,'pgid':group,'label':name} for p,group,name in self.processes]},indent=2))
        return process
    async def start(self):
        if not self.shared_state:self.listener=self.launch([sys.executable,'-m','dispatcher.waitd'],'listener')
        await eventually(lambda:(self.state/'wait/wait.sock').exists(),'SETUP_LISTENER_NOT_READY')
        snapshot=self.host.prepare(self.target,self.issue,self.stage,ticket=self.ticket,worktree=str(self.worktree))
        self.binding=snapshot['binding']
        prompt=self.directory/'prompt.txt';prompt.write_text('Generic initial stage work.')
        environment={**self.env,'AGENT_OPS_TARGET':self.target,'AGENT_OPS_ISSUE':str(self.issue),'AGENT_OPS_LAUNCH_ID':self.binding['launch_id'],'AGENT_OPS_CONVERSATION_ID':'','AGENT_OPS_STAGE':self.stage,'AGENT_OPS_TICKET':self.ticket}
        self.supervisor=self.launch([sys.executable,'-P','-m','dispatcher.codex_supervisor','--model','fixture-model','--prompt-file',str(prompt)],'supervisor',cwd=self.worktree,env=environment)
        await eventually(lambda:(self.run/'terminal-ready.json').exists(),'SETUP_TERMINAL_NOT_READY: inspect owned supervisor log')
        self.binding=self.view()['binding'];self.root=self.binding['conversation_id']
        assert self.root and self.view()['service']=='live','SETUP_BOUND_LIVE_SERVICE_MISSING'
        self.controller=await unix_connect(str(self.run/'control.sock'))
        self.native=await Client().open(json.loads((self.run/'ready.json').read_text())['native_socket']);self.clients.append(self.native)
        await self.native.rpc('initialize',{'clientInfo':{'name':'own-observation','version':'1'},'capabilities':{'experimentalApi':True}})
        await self.native.socket.send(json.dumps({'method':'initialized'}))
        await self.native.rpc('thread/resume',{'threadId':self.root})
        return self
    def view(self):return self.host.view(self.target,self.issue,self.binding['launch_id'])
    async def control(self,op,**fields):
        await self.controller.send(json.dumps({'op':op,**fields}));response=json.loads(await self.controller.recv());assert 'fixture_error' not in response,response;return response['result']
    async def terminal(self,op='read',**fields):
        async with unix_connect(str(self.run/'terminal.sock')) as socket:
            await socket.send(json.dumps({'op':op,**fields}));return json.loads(await socket.recv())
    def records(self):
        path=self.run/'captures.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    def result_requests(self):
        found=[]
        for entry in self.records():
            if entry['kind']!='client-request':continue
            packet=entry['payload']
            if packet.get('method') not in ['turn/start','turn/steer']:continue
            for item in packet.get('params',{}).get('input',[]):
                try:records=json.loads(item.get('text',''))
                except (ValueError,TypeError):continue
                if isinstance(records,list) and records and all(isinstance(record,dict) and set(record)=={'identity','outcome'} for record in records):found.append((packet,records))
        return found
    async def result(self,identity,timeout=4):
        key=identity_key(identity)
        return await eventually(lambda:next(((packet,record) for packet,records in self.result_requests() for record in records if identity_key(record['identity'])==key),None),'T6_NO_PROMPT_RESULT_TRANSMISSION',timeout)
    async def command(self,text='generic worker'):
        return await self.control('command-start',thread_id=self.root,command=text)
    async def qualify(self,commands):
        await self.control('complete-turn',thread_id=self.root)
        ids={command['item_id'] for command in commands}
        await eventually(lambda:ids.issubset({w['identity'].get('initial_item_id') for w in self.view()['workers'] if w.get('eligible')}),'SETUP_COMMAND_NOT_AUTHORITATIVELY_QUALIFIED')
    async def finish(self,command,status='completed',exit_code=0,output='generic output',duration=0):
        await self.control('command-complete',thread_id=self.root,item_id=command['item_id'],status=status,exit_code=exit_code,output=output,duration_ms=duration)
        return {'kind':'command','thread_id':self.root,'initial_item_id':command['item_id']}
    async def close(self):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        directory=ARTIFACTS/self.name;directory.mkdir(exist_ok=True)
        (directory/'before-teardown.json').write_text(json.dumps({'run_directory':str(self.directory),'home':self.env['HOME'],'state_directory':str(self.state),'runtime_parent':str(self.state/'runtime'),'worktree':str(self.worktree),'native_ready':json.loads((self.run/'ready.json').read_text()) if (self.run/'ready.json').exists() else None,'terminal_ready':json.loads((self.run/'terminal-ready.json').read_text()) if (self.run/'terminal-ready.json').exists() else None,'processes':[{'pid':p.pid,'pgid':g,'label':label} for p,g,label in self.processes]},indent=2)+'\n')
        for client in self.clients:await client.close()
        if hasattr(self,'controller'):await self.controller.close()
        # Only groups created by this fixture are signaled. Children inherit the
        # supervisor's group; verify every owned recorded PID belongs to a group.
        owned_pids=[]
        for name in ['ready.json','terminal-ready.json']:
            path=self.run/name
            if path.exists():owned_pids.append(json.loads(path.read_text())['pid'])
        groups={pgid for _,pgid,_ in self.processes}
        for pid in owned_pids:
            try:
                group=os.getpgid(pid)
                assert group in groups or group==pid,('UNDECLARED_CHILD_PROCESS_GROUP',pid,group)
                groups.add(group)
                # Let the supervisor reap its children before it is terminated.
                os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
        await asyncio.sleep(.05)
        for process,pgid,label in reversed(self.processes):
            try:process.terminate()
            except ProcessLookupError:pass
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
        for pgid in groups:
            try:os.killpg(pgid,signal.SIGTERM)
            except ProcessLookupError:pass
            except PermissionError:
                # EPERM is not success: only eventual ESRCH below counts clean.
                pass
        deadline=time.monotonic()+8
        while True:
            live=[]
            for pgid in groups:
                try:os.killpg(pgid,0);live.append(pgid)
                except ProcessLookupError:pass
            if not live:break
            assert time.monotonic()<deadline,('OWNED_GROUP_NOT_GONE',live)
            await asyncio.sleep(.025)
        for log in self.logs:log.close()
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        directory=ARTIFACTS/self.name;directory.mkdir(exist_ok=True)
        for path in [*self.directory.glob('*.log'),*self.run.glob('*.json'),self.run/'captures.jsonl',self.run/'argv.jsonl']:
            if path.exists():(directory/path.name).write_bytes(path.read_bytes())
        (directory/'cleanup.json').write_text(json.dumps({'groups':sorted(groups),'all_groups_absent':True,'owned_pids':owned_pids,'process_returncodes':{label:process.returncode for process,_,label in self.processes},'run_directory':str(self.directory)},indent=2))
        import shutil
        shutil.rmtree(self.directory)
        assert not self.directory.exists(),'OWNED_TEMP_DIRECTORY_SURVIVES'
        (directory/'filesystem-cleanup.json').write_text(json.dumps({'run_directory':str(self.directory),'run_directory_absent':True,'private_sockets_absent':True,'all_groups_absent':True},indent=2)+'\n')
        if (directory/'captures.jsonl').exists():
            from .packet_audit import audit_capture
            (directory/'packet-audit.json').write_text(json.dumps(audit_capture(directory/'captures.jsonl'),indent=2)+'\n')
