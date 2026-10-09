"""External public dependency doubles retain real Sessions/run_pass/listener."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
from dispatcher import usage_providers
from dispatcher.config import Config,Target
from dispatcher.main import Deps,run_pass
from dispatcher.models import DEFAULT_POLICY
from dispatcher.state import save,load
from dispatcher.usage import ProviderUsage,Window,WindowKind

class LocalGitHub:
    def __init__(self):self.statuses=[]
    def viewer_login(self):return 'fixture'
    def rank_rows(self,target):return []
    def candidates(self,target):return []
    def issue_state(self,repo,number):return 'OPEN'
    def issue_view(self,repo,number):return {'number':number,'title':'Generic task','body':'Generic fixture issue','labels':[]}
    def set_status(self,target,issue,option_id):self.statuses.append((issue,option_id))
    def pr_number_for_branch(self,target,branch):return 123
    def pr_view(self,target,pr_number):return {'number':pr_number,'state':'OPEN','headRefName':'fixture','headRefOid':'generic','url':'https://github.com/fixture/repo/pull/123','mergeable':'MERGEABLE'}
    def ci_statuses(self,target,head,branch):return []
    def run_status(self,target,run_id):return ''
    def __getattr__(self,name):
        if name in ['set_boost','add_label','comment','claim','release','cancel','delete_branch','append_blocked_by']:return lambda *args,**kwargs:None
        raise AttributeError(name)
class LocalNotifier:
    def __init__(self):self.sent=[]
    def send(self,template,**context):self.sent.append((template,context));return 123
class LocalUsage:
    def __init__(self,name):self.name=name
    def fetch(self,state_dir,*,now=time.time):
        future=datetime.now(timezone.utc)+timedelta(days=7)
        return ProviderUsage(provider=self.name,source='oauth',fetched_at=now(),windows=(Window(kind=WindowKind.SESSION,scope=None,used=0,resets_at=future),Window(kind=WindowKind.WEEKLY,scope=None,used=0,resets_at=future)))
class PhysicalTab:
    def __init__(self,flow):self.flow=flow;self.closed=[];self.commands=[]
    @property
    def alive(self):return self.flow.supervisor.poll() is None
    def run(self,command):self.commands.append(command);return True
    def read(self,source,lines):return 'Generic terminal output\n'
    def agent_state(self):return ('working',1)
    def send_text(self,text):return True
    def send_keys(self,*keys):return True
    def close(self):
        self.closed.append(self.flow.view())
        self.flow.supervisor.terminate()
        return True

class DispatcherFixture:
    def __init__(self,flow):
        self.flow=flow;self.tab=PhysicalTab(flow);self.github=LocalGitHub();self.notifier=LocalNotifier()
        target=Target(name=flow.target,repo='fixture/repo',clone_path=str(flow.worktree),worktrees_path=str(flow.directory/'worktrees'),rank_cmd='',project_number=1,project_owner='fixture',status_field_id='status',status_ready_option_id='ready',status_in_progress_option_id='in-progress')
        self.config=Config(state_dir=str(flow.state),capacity=1,session_memory='2g',session_cpus='2',targets=[target],stall_after_seconds=100000,background_wait_seconds=10800,spec_review_grace_minutes=0)
        flow.task=replace(flow.task,track=DEFAULT_POLICY.untracked,updated_at=datetime.now(timezone.utc).isoformat());save(flow.state,flow.task)
        self.real_run=subprocess.run
    def pass_once(self):
        def process(args,*other,**kwargs):
            if isinstance(args,(list,tuple)) and len(args)>=3 and list(args[:3])==['podman','rm','-f']:
                return subprocess.CompletedProcess(args,0,stdout='',stderr='')
            return self.real_run(args,*other,**kwargs)
        adapters={entry.provider:LocalUsage(entry.provider) for entry in DEFAULT_POLICY.entries()}
        with patch('dispatcher.herdr.Tab.find',return_value=self.tab),patch('dispatcher.herdr.Tab.ensure',return_value=self.tab),patch('subprocess.run',side_effect=process),patch.dict(usage_providers.ADAPTERS,adapters),patch('telegram.inbound.fetch_events',return_value=[]):
            run_pass(self.config,Deps(github=self.github,sessions=self.flow.sessions,notifier=self.notifier))
        return load(self.flow.state,self.flow.target,self.flow.issue)
