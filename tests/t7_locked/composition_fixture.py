"""Own disclosed public composition host; no private Controller access or fallback."""
import asyncio
import copy
import hashlib
import os
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import unix_serve
from dispatcher import codex_supervisor
from dispatcher.codex_transport import Gateway
from dispatcher.runtime_http import BoundClient
from dispatcher.runtime_control import RuntimeControl
from .http_fault_proxy import HTTPFaultProxy
from .external_fixture import ExternalFixture,eventually,ARTIFACTS
from .native_client import Client

class CompositionFixture(ExternalFixture):
    def __init__(self,legacy_bootstrap=False,**kwargs):
        super().__init__(**kwargs);self.legacy_bootstrap=legacy_bootstrap;self.proxy=None;self.attachments=[];self.phases=[];self.gateway_server=None
        self.arguments=SimpleNamespace(model='fixture-model',effort='',prompt='  Generic initial λ\n');self.bootstrap_lock=asyncio.Lock()
    def phase(self,name,**fields):
        self.phases.append({'phase':name,**fields})
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        (ARTIFACTS/(self.name+'-composition.json')).write_text(json.dumps(self.phases,indent=2)+'\n')
    async def start(self):
        legacy=None
        if self.legacy_bootstrap:
            control=RuntimeControl(self.state);original=control.prepare(self.target,self.issue,self.stage,ticket=self.ticket,worktree=str(self.worktree));legacy=copy.deepcopy(original);legacy.pop('bootstrap',None)
            key=hashlib.sha256((self.target+'\0'+str(self.issue)).encode()).hexdigest();directory=self.state/'runtime'/key;launch=directory/(legacy['binding']['launch_id']+'.json');pointer=directory/'current.json';pointer_before=pointer.read_bytes();temporary=launch.with_suffix('.fixture.tmp');temporary.write_text(json.dumps(legacy,allow_nan=False));os.replace(temporary,launch);assert pointer.read_bytes()==pointer_before
            self.binding=legacy['binding'];self.phase('offline-legacy-bootstrap-absence',original_public_view=original,legacy_public_view=legacy,preserved_pointer=True)
        self.listener=self.launch([sys.executable,'-m','dispatcher.waitd'],'listener')
        await eventually(lambda:(self.state/'wait/wait.sock').exists(),'SETUP_T7_LISTENER_NOT_READY')
        if legacy is None:self.binding=self.host.prepare(self.target,self.issue,self.stage,ticket=self.ticket,worktree=str(self.worktree))['binding']
        else:assert self.host.view(self.target,self.issue,self.binding['launch_id'])==legacy,'SETUP_LEGACY_PUBLIC_VIEW_NOT_EXACT'
        self.backend=self.launch([sys.executable,str(Path(__file__).with_name('provider.py')),'--run-dir',str(self.run)],'backend')
        await eventually(lambda:(self.run/'ready.json').exists(),'SETUP_T7_NATIVE_PROVIDER_NOT_READY')
        self.backend_path=json.loads((self.run/'ready.json').read_text())['native_socket']
        self.controller=await unix_connect(str(self.run/'control.sock'))
        self.native=await Client().open(self.backend_path);self.clients.append(self.native)
        response=await self.native.rpc('initialize',{'clientInfo':{'name':'own-observation','version':'1'},'capabilities':{'experimentalApi':True}})
        assert 'result' in response,'SETUP_T7_NATIVE_INITIALIZE_FAILED'
        await self.native.socket.send(json.dumps({'method':'initialized'}))
        assert self.host.event(self.binding,{'type':'service','status':'live'}),'SETUP_T7_SERVICE_OWNER_FAILED'
        self.bound=BoundClient(self.state,self.target,self.issue,self.binding['launch_id'])
        assert self.bound.view()['binding']==self.binding,'SETUP_T7_EXACT_PUBLIC_VIEW_MISSING'
        self.phase('healthy-listener-provider',backend_pid=self.backend.pid,binding=copy.deepcopy(self.binding));return self
    def factory(self):
        factory=getattr(codex_supervisor,'attach_controller',None)
        if not callable(factory):self.phase('attachment-unavailable',reason='Declared attach_controller capability absent after healthy native/listener setup.')
        return factory
    async def enter(self,factory,mode='attach',ready=True):
        before=set((await self.control('connections'))['connection_ids'])
        self.binding=self.view()['binding']
        lock=self.bootstrap_lock if mode=='first-launch' or not hasattr(self,'gateway') else self.gateway.input_lock
        manager=factory(self.bound,self.binding,self.arguments,backend_path=self.backend_path,input_lock=lock,mode=mode)
        handle=await manager.__aenter__();entry={'manager':manager,'handle':handle,'mode':mode,'connections':set(),'before_connections':before};self.attachments.append(entry)
        self.phase('attachment-entered',mode=mode)
        if ready:await self.ready(entry)
        entry['connections']=set((await self.control('connections'))['connection_ids'])-before
        return entry
    async def ready(self,entry,timeout=8):
        try:binding=await asyncio.wait_for(entry['handle'].wait_ready(),timeout)
        except TimeoutError as error:raise AssertionError('T7_REAL_ATTACHMENT_READINESS_MISSING') from error
        self.binding=self.bound.view()['binding'];self.root=self.binding['conversation_id']
        entry['connections']=set((await self.control('connections'))['connection_ids'])-entry['before_connections']
        assert binding==self.binding and self.root,'T7_ATTACHMENT_RETURNED_FOREIGN_OR_INCOMPLETE_READY_BINDING'
        self.phase('attachment-ready',mode=entry['mode'],binding=copy.deepcopy(binding));return binding
    async def exit(self,entry):
        if entry not in self.attachments:return
        entry['connections']|=set((await self.control('connections'))['connection_ids'])-entry['before_connections']
        failure=None
        try:
            try:await asyncio.wait_for(entry['manager'].__aexit__(None,None,None),8)
            except (RuntimeError,ValueError,ConnectionError) as error:failure=error
            try:await asyncio.wait_for(entry['handle'].wait_closed(),8)
            except (RuntimeError,ValueError,ConnectionError) as error:failure=error
        except TimeoutError as error:raise AssertionError('T7_ATTACHMENT_NOT_QUIESCENT_ON_EXIT') from error
        self.attachments.remove(entry)
        async def owned_connections_removed():
            return not entry['connections']&set((await self.control('connections'))['connection_ids'])
        await eventually(owned_connections_removed,'T7_OWN_CONTROLLER_CONNECTION_SURVIVES_EXIT',timeout=8)
        connections=set((await self.control('connections'))['connection_ids'])
        assert not entry['connections']&connections,'T7_OWN_CONTROLLER_CONNECTION_SURVIVES_EXIT'
        self.phase('attachment-quiescent-exit',mode=entry['mode'],connections=sorted(entry['connections']))
        if failure is not None:raise failure
    async def first_launch(self,factory):
        entry=await self.enter(factory,mode='first-launch');binding=self.binding;await self.exit(entry);return binding
    async def start_terminal(self):
        assert not self.attachments,'T7_FIRST_LAUNCH_NOT_EXITED_BEFORE_GATEWAY'
        self.gateway=Gateway(self.backend_path,self.root,self.bound,self.binding)
        self.gateway_path=self.run/'gateway.sock';self.gateway_server=await unix_serve(self.gateway.serve,path=str(self.gateway_path))
        self.supervisor=self.launch([str(self.directory/'bin/codex'),'--remote','unix://'+str(self.gateway_path),'-C',str(self.worktree),'resume','--',self.root],'terminal')
        await eventually(lambda:(self.run/'terminal-ready.json').exists(),'SETUP_T7_REAL_REMOTE_TERMINAL_NOT_READY')
        terminal=json.loads((self.run/'terminal-ready.json').read_text());assert terminal['root']==self.root and terminal['gateway']==str(self.gateway_path),'T7_TERMINAL_NOT_BOUND_TO_PUBLIC_GATEWAY'
        self.phase('gateway-terminal-live',backend_pid=self.backend.pid,terminal_pid=self.supervisor.pid,root=self.root,gateway=str(self.gateway_path));return self.gateway
    async def ready_composition(self,factory):
        await self.first_launch(factory);await self.start_terminal();entry=await self.enter(factory);return entry
    async def use_proxy(self):
        assert not self.attachments,'OWN_PROXY_SELECTED_AFTER_ATTACHMENT_START'
        proxy_state=self.directory/'proxy';(proxy_state/'wait').mkdir(parents=True);config=proxy_state/'rules.json';config.write_text('[]');socket=proxy_state/'wait/wait.sock'
        process=self.launch([sys.executable,str(Path(__file__).with_name('http_fault_proxy.py')),'--socket',str(socket),'--upstream',str(self.state/'wait/wait.sock'),'--config',str(config),'--evidence',str(ARTIFACTS/(self.name+'-http-proxy.jsonl'))],'http-proxy')
        await eventually(lambda:process.poll() is None and socket.exists(),'SETUP_OWN_HTTP_PROXY_NOT_READY');self.proxy=ProxyControl(config,process)
        self.bound=BoundClient(proxy_state,self.target,self.issue,self.binding['launch_id']);assert self.bound.view()==self.view();return self.proxy
    async def restart_listener(self):
        old=self.listener;old.terminate();old.wait(timeout=8)
        assert old.poll() is not None,'SETUP_OWN_LISTENER_NOT_STOPPED'
        self.listener=self.launch([sys.executable,'-m','dispatcher.waitd'],'listener-restarted')
        await eventually(lambda:self.listener.poll() is None and (self.state/'wait/wait.sock').exists(),'SETUP_RESTARTED_LISTENER_NOT_READY')
        def readable():
            try:return self.bound.view() is not None
            except (ConnectionError,OSError):return False
        await eventually(readable,'SETUP_RESTARTED_EXACT_VIEW_NOT_READY')
        self.phase('real-listener-restarted',old_pid=old.pid,new_pid=self.listener.pid)
    async def close(self):
        # Preserve own public transport/ownership metadata before original strict teardown.
        self.phase('before-owned-cleanup',backend_pid=getattr(getattr(self,'backend',None),'pid',None),native_socket=getattr(self,'backend_path',None),gateway_socket=str(getattr(self,'gateway_path','')),state_directory=str(self.state),runtime_parent=str(self.state/'runtime'))
        for entry in list(reversed(self.attachments)):await self.exit(entry)
        if self.gateway_server is not None:
            self.gateway_server.close();await self.gateway_server.wait_closed();self.gateway_path.unlink(missing_ok=True)
        if self.proxy is not None:await self.proxy.close()
        await super().close()

class ProxyControl:
    def __init__(self,path,process):self.path=path;self.process=process;self.rules=[]
    def arm(self,event_type=None,route=None,mode='drop-response'):
        rule={'id':'own-http-rule-'+str(len(self.rules)+1),'event_type':event_type,'route':route,'mode':mode};self.rules.append(rule);self.path.write_text(json.dumps(self.rules));return rule
    def hit(self,rule):
        path=self.path.with_suffix('.hits.json');return path.exists() and rule['id'] in json.loads(path.read_text())
    async def close(self):
        self.process.terminate();self.process.wait(timeout=8);assert self.process.returncode==0,'OWN_HTTP_PROXY_NONCLEAN_EXIT'
