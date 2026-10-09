"""Real listener/runtime/Sessions setup without an automatic delivery actor.

The owned physical terminal process is an external herdr double. Native wire delivery
remains covered by the separate genuine supervisor/gateway cases.
"""
import copy
import json
import sys
import time
from .external_fixture import ExternalFixture,ARTIFACTS,eventually

class LayeredDispatcherFixture(ExternalFixture):
    async def start(self):
        self.listener=self.launch([sys.executable,'-m','dispatcher.waitd'],'listener')
        await eventually(lambda:(self.state/'wait/wait.sock').exists(),'SETUP_LAYERED_LISTENER_NOT_READY')
        self.root='root-layer-generic';self.turn='turn-layer-generic';self.workers=[];self.events=[]
        self.binding=self.host.prepare(self.target,self.issue,self.stage,conversation_id=self.root,worktree=str(self.worktree))['binding']
        # A disposable physical session supplies only external Tab.alive/close.
        # It has no dispatcher/runtime/controller imports or delivery actor.
        self.supervisor=self.launch([sys.executable,'-c','import time; time.sleep(3600)'],'supervisor')
        self.apply({'type':'service','status':'live'})
        admitted=self.host.accept_input(self.binding,'layer-initial-client');assert admitted,'PUBLIC_LAYERED_BOOTSTRAP_ADMISSION_REJECTED'
        self.apply({'type':'turn/started','thread_id':self.root,'turn_id':self.turn})
        self.apply({'type':'input/accepted','client_message_id':'layer-initial-client','turn_id':self.turn})
        return self

    def apply(self,event,now=None):
        accepted=self.host.event(self.binding,event,now=now)
        self.events.append({'binding':self.binding,'event':copy.deepcopy(event),'now':now,'accepted':accepted})
        assert accepted is True,('PUBLIC_LAYERED_SETUP_EVENT_REJECTED',event)
        return self.view()

    def inventory(self,now=None):
        return self.apply({'type':'inventory','certainty':'known','workers':copy.deepcopy(self.workers),'observed_revision':self.view()['revision']},now)

    async def command(self,text='generic worker'):
        item='item-layer-'+str(len(self.workers)+1)
        worker={'identity':{'kind':'command','thread_id':self.root,'initial_item_id':item},'turn_id':self.turn,
                'ancestry':[{'thread_id':self.root,'parent_thread_id':None,'source_kind':'bound-root','source_parent_thread_id':None,'depth':0}],
                'process_id':'process-layer-'+str(len(self.workers)+1),'command':text,'cwd':str(self.worktree),'source':'unifiedExecStartup',
                'status':'running','inventory_running':True,'normal_stop':None,'outcome':None}
        self.workers.append(worker);self.inventory();return {'item_id':item,'turn_id':self.turn}

    async def qualify(self,commands,now=None):
        now=time.time() if now is None else now
        self.apply({'type':'turn/completed','thread_id':self.root,'turn_id':self.turn,'status':'completed'},now)
        revision=self.view()['main']['completed_turns'][self.turn]
        for worker in self.workers:
            if worker['identity']['initial_item_id'] in {command['item_id'] for command in commands}:
                worker['normal_stop']={'thread_id':self.root,'turn_id':self.turn,'status':'completed','revision':revision}
        self.inventory(now)
        assert all(worker['eligible'] for worker in self.view()['workers']),'PUBLIC_LAYERED_COMMAND_QUALIFICATION_MISSING'

    async def finish(self,command,status='completed',exit_code=0,output='generic output',duration=0):
        worker=next(worker for worker in self.workers if worker['identity']['initial_item_id']==command['item_id'])
        worker.update(status=status,inventory_running=False,outcome={'status':status,'exit_code':exit_code,'aggregated_output':output,'duration_ms':duration});self.inventory()
        return copy.deepcopy(worker['identity'])

    async def close(self):
        events=copy.deepcopy(getattr(self,'events',[]));await super().close()
        (ARTIFACTS/self.name/'layered-public-events.json').write_text(json.dumps(events,indent=2)+'\n')
