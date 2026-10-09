"""Own setup through the approved public listener-state boundary only."""
import copy
import json
from pathlib import Path
import tempfile
from dispatcher.runtime_control import RuntimeControl
from dispatcher.state import Stage, TaskState, save


def identity_key(identity):
    return json.dumps(identity,sort_keys=True,separators=(',',':'))


def result_input(records,prose=True):
    content=[{'type':'text','text':json.dumps(records,indent=1,ensure_ascii=False)}]
    return ([{'type':'text','text':'Available background outcomes.'}]+content) if prose else content


class ContractFixture:
    def __init__(self,stage='review',ticket='',issue=101,publish=True):
        self.temp=tempfile.TemporaryDirectory(prefix='t7c-')
        self.home=Path(self.temp.name)
        self.state=self.home/'state'
        self.worktree=self.home/'task'
        self.worktree.mkdir()
        (self.worktree/'.agent').mkdir()
        self.target='fixture'
        self.issue=issue
        self.stage=stage
        self.ticket=ticket
        self.root='root-generic'
        self.control=RuntimeControl(self.state)
        snapshot=self.control.prepare(self.target,issue,stage,ticket=ticket,conversation_id=self.root,worktree=str(self.worktree))
        self.binding=snapshot['binding']
        self.task_state=TaskState(issue=issue,target=self.target,stage=Stage(stage),slot=0,worktree=str(self.worktree),branch='fixture',title='Generic task',updated_at='2026-10-08T00:00:00Z',ticket_cursor=int(ticket or 0),ticket_count=max(int(ticket or 0),1))
        if publish:self.publish_task()
        self.signal()
        self.apply({'type':'service','status':'live'})
        self.apply({'type':'turn/started','thread_id':self.root,'turn_id':'main-a'})
        self.apply({'type':'turn/completed','thread_id':self.root,'turn_id':'main-a','status':'completed'})
        self.stop_revision=self.view()['main']['completed_turns']['main-a']
        self.records=[]
        self.workers=[]

    def close(self):
        self.temp.cleanup()

    def view(self):
        return self.control.view(self.target,self.issue,self.binding['launch_id'])

    def apply(self,event,now=10,binding=None):
        accepted=self.control.event(binding or self.binding,event,now=now)
        assert accepted is True,('PUBLIC_SETUP_EVENT_REJECTED',event,accepted)
        return self.view()

    def signal(self,status='working',stage=None):
        (self.worktree/'.agent/stage.json').write_text(json.dumps({'stage':stage or self.stage,'status':status}))

    def publish_task(self):
        save(self.state,self.task_state)

    def completion(self,item='command-a',status='completed',exit_code=0,output='generic output',duration=0):
        identity={'kind':'command','thread_id':self.root,'initial_item_id':item}
        running={'identity':identity,'turn_id':'main-a','ancestry':[{'thread_id':self.root,'parent_thread_id':None,'source_kind':'bound-root','source_parent_thread_id':None,'depth':0}],
            'process_id':'process-generic','command':'generic worker','cwd':str(self.worktree),'source':'unifiedExecStartup',
            'status':'running','inventory_running':True,'normal_stop':{'thread_id':self.root,'turn_id':'main-a','status':'completed','revision':self.stop_revision},'outcome':None}
        self.workers.append(running)
        self.inventory(now=10)
        outcome={'status':status,'exit_code':exit_code,'aggregated_output':output,'duration_ms':duration}
        running.update(status=status,inventory_running=False,outcome=outcome)
        self.inventory(now=20)
        record={'identity':copy.deepcopy(identity),'outcome':copy.deepcopy(outcome)}
        assert record in self.view()['completions'],('PUBLIC_SETUP_OUTCOME_NOT_RECORDED',self.view())
        self.records.append(record)
        return record

    def inventory(self,now=20,certainty='known',checkpoint=None):
        event={'type':'inventory','certainty':certainty,'workers':copy.deepcopy(self.workers),'observed_revision':self.view()['revision']}
        if checkpoint is not None:event['history_checkpoint']=checkpoint
        return self.apply(event,now=now)

    def active(self,turn='main-b'):
        return self.apply({'type':'turn/started','thread_id':self.root,'turn_id':turn},now=30)

    def stop(self,turn='main-b'):
        return self.apply({'type':'turn/completed','thread_id':self.root,'turn_id':turn,'status':'completed'},now=40)

    def proposal(self,batch='batch-generic',records=None,revision=None,content=None):
        records=self.records if records is None else records
        return {'type':'delivery/proposed','observed_revision':self.view()['revision'] if revision is None else revision,'batch_id':batch,'completion_ids':[r['identity'] for r in records],'input':result_input(records) if content is None else content}

    def propose(self,batch='batch-generic',**fields):
        event=self.proposal(batch=batch,**fields)
        assert self.control.event(self.binding,event,now=30) is True,'T6_MISSING_VALID_DELIVERY_PROPOSAL'
        return event

    def send(self,batch='batch-generic',attempt='attempt-generic',client='client-generic',method='turn/steer',expected='main-b'):
        event={'type':'delivery/sent','observed_revision':self.view()['revision'],'batch_id':batch,'attempt_id':attempt,'client_message_id':client,'method':method,'thread_id':self.root,'expected_turn_id':expected}
        assert self.control.event(self.binding,event,now=31) is True,'T6_MISSING_ATOMIC_DELIVERY_SEND'
        return event

    def ack(self,batch='batch-generic',attempt='attempt-generic',client='client-generic',turn='main-b'):
        event={'type':'delivery/ack','batch_id':batch,'attempt_id':attempt,'client_message_id':client,'thread_id':self.root,'turn_id':turn}
        assert self.control.event(self.binding,event,now=32) is True,'T6_MISSING_CORRELATED_DELIVERY_ACK'
        return event

    def batch(self,batch='batch-generic'):
        return next(b for b in self.view()['deliveries'] if b['batch_id']==batch)
