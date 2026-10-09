#!/usr/bin/env python3
"""Independent native-schema fake for locked tests. No product dependencies."""
import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import signal
import time
from websockets.asyncio.server import unix_serve
from websockets.exceptions import ConnectionClosed
from schema_check import Schemas, SchemaError

SCHEMAS = str(Path(__file__).with_name('schemas'))
RPC = {
    'initialize': 'Initialize',
    'thread/start': 'ThreadStart', 'thread/resume': 'ThreadResume',
    'thread/read': 'ThreadRead', 'thread/list': 'ThreadList',
    'thread/items/list': 'ThreadItemsList', 'thread/turns/list': 'ThreadTurnsList',
    'thread/backgroundTerminals/list': 'ThreadBackgroundTerminalsList',
    'turn/start': 'TurnStart', 'turn/steer': 'TurnSteer',
}


def clone(value):
    return copy.deepcopy(value)


def uid(kind):
    return kind + '-' + secrets.token_hex(8)


class RpcFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Provider:
    def __init__(self, directory, schemas=SCHEMAS, native_socket=None):
        self.directory = Path(directory)
        self.native_socket = Path(native_socket) if native_socket else self.directory/'native.sock'
        self.schemas = Schemas(schemas)
        self.threads = {}
        self.connections = {}
        self.subscriptions = {}
        self.cursors = {}
        self.barriers = {}
        self.faults = []
        self.reply_faults = []
        self.empty_item_threads = set()
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.sockets = []

    def record(self, kind, **fields):
        self.sequence += 1
        entry = {'sequence': self.sequence, 'kind': kind, **clone(fields)}
        with (self.directory/'captures.jsonl').open('a') as handle:
            handle.write(json.dumps(entry, sort_keys=True) + '\n')

    def thread(self, thread_id):
        try:
            return self.threads[thread_id]
        except KeyError:
            raise RpcFailure(-32600, 'fixture thread not found')

    def metadata(self, thread, hydrate=False):
        data = clone(thread['metadata'])
        data['turns'] = clone(thread['turns']) if hydrate else []
        return data

    def create_thread(self, cwd=None, parent=None):
        thread_id = uid('thread')
        stamp = int(time.time())
        source = 'vscode'
        depth = 0
        if parent:
            owner = self.thread(parent)
            depth = owner['depth'] + 1
            source = {'subAgent': {'thread_spawn': {'depth': depth, 'parent_thread_id': parent}}}
        metadata = {'id': thread_id, 'cliVersion': '0.156.1', 'createdAt': stamp,
            'updatedAt': stamp, 'cwd': cwd or str(self.directory/'workspace'),
            'ephemeral': False, 'modelProvider': 'fixture', 'preview': '',
            'projectId': None, 'sessionId': self.thread(parent)['metadata']['sessionId'] if parent else uid('session'),
            'source': source, 'parentThreadId': parent, 'status': {'type': 'idle'},
            'turns': [], 'canAcceptDirectInput': True}
        self.threads[thread_id] = {'metadata': metadata, 'turns': [], 'terminals': {}, 'depth': depth}
        return self.threads[thread_id]

    def response_thread(self, thread, hydrate=False):
        return {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
            'cwd': thread['metadata']['cwd'], 'model': 'fixture-model',
            'modelProvider': 'fixture', 'sandbox': {'type': 'readOnly'},
            'thread': self.metadata(thread, hydrate)}

    def active(self, thread):
        return next((turn for turn in reversed(thread['turns']) if turn['status']=='inProgress'), None)

    async def notify(self, method, params):
        payload = {'method': method, 'params': clone(params)}
        self.schemas.validate('ServerNotification.json', payload)
        self.schemas.validate('JSONRPCNotification.json', payload)
        thread_id = params.get('threadId', params.get('thread', {}).get('id'))
        for connection_id, socket in list(self.connections.items()):
            if thread_id not in self.subscriptions.get(connection_id,set()):
                continue
            try:
                await socket.send(json.dumps(payload))
                self.record('server-notification', connection_id=connection_id, payload=payload)
            except ConnectionClosed:
                pass

    async def hit(self, point, method, thread_id, client_message_id=None):
        for key, barrier in list(self.barriers.items()):
            if barrier['one_shot'] and barrier['hits']:
                continue
            if barrier['point']==point and barrier['method'] in [None, method] and barrier['thread_id'] in [None, thread_id] and barrier['client_message_id'] in [None, client_message_id]:
                barrier['hits'] += 1
                self.record('barrier-hit', barrier_id=key, point=point, method=method, thread_id=thread_id, client_message_id=client_message_id)
                await barrier['released'].wait()

    async def fault(self, point, method, socket, request):
        match = next((fault for fault in self.faults if fault['point']==point and fault['method']==method), None)
        if match is None:
            return False
        self.faults.remove(match)
        self.record('fault', point=point, method=method, request_id=request['id'],
            certainty='proven-nonacceptance' if point=='before-native-operation' else 'uncertain-acceptance')
        await socket.close(code=1011, reason='isolated fixture scheduled fault')
        return True

    def page(self, method, params, data, default_direction='asc'):
        direction = params.get('sortDirection') or default_direction
        scope = (method, params.get('threadId'), params.get('turnId'),
            params.get('ancestorThreadId'), params.get('parentThreadId'),
            json.dumps(params.get('sourceKinds'), sort_keys=True))
        ordered = list(reversed(data)) if direction == 'desc' else list(data)
        start = 0
        if params.get('cursor'):
            cursor = self.cursors.get(params['cursor'])
            if cursor is None or cursor['scope'] != scope:
                raise RpcFailure(-32602, 'fixture cursor has another scope')
            if cursor['direction'] == direction:
                start = cursor['index']
            else:
                # Reverse traversal includes the first anchor of the prior page.
                start = len(data)-1-cursor['anchor'] if direction=='desc' else cursor['anchor']
        limit = params.get('limit')
        limit = 2 if limit is None else limit
        if limit == 0:
            raise RpcFailure(-32602, 'fixture zero page size unsupported')
        selected = ordered[start:start+limit]
        def token(index, anchor):
            opaque = uid('cursor')
            self.cursors[opaque] = {'scope': scope, 'direction': direction, 'index': index, 'anchor': anchor}
            return opaque
        anchor = len(data)-1-start if direction=='desc' else start
        result = {'data': clone(selected), 'nextCursor': token(start+limit, anchor) if start+limit<len(data) else None}
        if method != 'thread/backgroundTerminals/list':
            result['backwardsCursor'] = token(start, anchor) if selected else None
        return result

    def is_descendant(self, thread, ancestor):
        parent = thread['metadata']['parentThreadId']
        while parent:
            if parent==ancestor:
                return True
            parent = self.thread(parent)['metadata']['parentThreadId']
        return False

    def source_kind(self, thread):
        source = thread['metadata']['source']
        return source if isinstance(source, str) else 'subAgentThreadSpawn'

    async def operation(self, connection_id, method, params):
        if method=='initialize':
            return {'codexHome':str(self.directory/'codex-home'),'platformFamily':'unix','platformOs':'macos','userAgent':'isolated-fixture/0.156.1'}
        if method=='thread/start':
            thread = self.create_thread(params.get('cwd'))
            self.subscriptions[connection_id].add(thread['metadata']['id'])
            await self.notify('thread/started', {'thread': self.metadata(thread)})
            return self.response_thread(thread)
        if method=='thread/list':
            if params.get('ancestorThreadId') and params.get('parentThreadId'):
                raise RpcFailure(-32602, 'fixture ancestry filters are mutually exclusive')
            threads = list(self.threads.values())
            sources = params.get('sourceKinds') or ['cli','vscode','appServer']
            threads = [t for t in threads if self.source_kind(t) in sources]
            if params.get('ancestorThreadId'):
                threads = [t for t in threads if self.is_descendant(t,params['ancestorThreadId'])]
            if params.get('parentThreadId'):
                threads = [t for t in threads if t['metadata']['parentThreadId']==params['parentThreadId']]
            if params.get('archived'):
                threads = []
            if params.get('cwd'):
                paths = params['cwd'] if isinstance(params['cwd'],list) else [params['cwd']]
                threads = [t for t in threads if t['metadata']['cwd'] in paths]
            if params.get('modelProviders'):
                threads = [t for t in threads if t['metadata']['modelProvider'] in params['modelProviders']]
            return self.page(method,params,[self.metadata(t) for t in threads],'desc')
        thread = self.thread(params['threadId'])
        if method=='thread/resume':
            if params.get('path') or params.get('history'):
                raise RpcFailure(-32602, 'fixture exact-ID resume only')
            self.subscriptions[connection_id].add(params['threadId'])
            if thread['metadata']['status']['type']=='notLoaded':
                thread['metadata']['status']={'type':'active','activeFlags':[]} if self.active(thread) else {'type':'idle'}
            return self.response_thread(thread,True)
        if method=='thread/read':
            return {'thread': self.metadata(thread,params.get('includeTurns',False))}
        if method=='thread/backgroundTerminals/list':
            return self.page(method,params,list(thread['terminals'].values()))
        if method=='thread/turns/list':
            turns = clone(thread['turns'])
            for turn in turns:
                view = params.get('itemsView') or 'summary'
                if view!='full':
                    turn['items']=[]
                turn['itemsView']=view
            return self.page(method,params,turns,'desc')
        if method=='thread/items/list' and params['threadId'] in self.empty_item_threads:
            return {'data':[],'nextCursor':None,'backwardsCursor':None}
        if method=='thread/items/list':
            # Conservative capture model: only completed-turn input receipts visible.
            entries = [{'turnId':turn['id'],'item':item} for turn in thread['turns']
                if turn['status']!='inProgress' and (not params.get('turnId') or params['turnId']==turn['id'])
                for item in turn['items']]
            return self.page(method,params,entries)
        active = self.active(thread)
        if method=='turn/steer':
            expected = params['expectedTurnId']
            if active is None:
                # Native idle literal is only modeled from an exact known idle
                # state with this request's nonempty recorded precondition.
                if expected and thread['metadata']['status']['type']=='idle':
                    raise RpcFailure(-32600, 'no active turn to steer')
                raise RpcFailure(-32600, 'fixture no active turn; unclassified nonacceptance')
            if expected != active['id']:
                # This must precede every native acceptance mutation.
                raise RpcFailure(-32600, f"expected active turn id `{expected}` but found `{active['id']}`")
        if method=='turn/start' and active is None:
            active = {'id':uid('turn'),'items':[],'itemsView':'full','status':'inProgress','error':None}
            thread['turns'].append(active)
            thread['metadata']['status']={'type':'active','activeFlags':[]}
            await self.notify('turn/started',{'threadId':params['threadId'],'turn':{**clone(active),'itemsView':'summary'}})
        message = {'type':'userMessage','id':uid('item'),'clientId':params.get('clientUserMessageId'),'content':clone(params['input'])}
        active['items'].append(message)
        self.record('native-accepted',method=method,thread_id=params['threadId'],turn_id=active['id'],item=message)
        return {'turnId':active['id']} if method=='turn/steer' else {'turn':clone(active)}

    async def native_request(self, socket, connection_id, request):
        method=request.get('method')
        if method=='initialized':
            self.schemas.validate('ClientNotification.json',request)
            return
        try:
            self.schemas.validate('JSONRPCRequest.json',request)
            self.schemas.validate('ClientRequest.json',request)
            if method not in RPC:
                raise RpcFailure(-32601,'fixture method unsupported')
            params=request.get('params',{})
            self.schemas.validate(('v1/' if method=='initialize' else 'v2/')+RPC[method]+'Params.json',params)
            if await self.fault('before-native-operation',method,socket,request):
                return
            if method=='thread/read':
                await self.hit('root-read',method,params.get('threadId'))
            if method in ['turn/start','turn/steer']:
                await self.hit('before-input-validation',method,params.get('threadId'),params.get('clientUserMessageId'))
            # Only native validation/state mutation is serialized; an ACK hold
            # never prevents another correlated request or inventory read.
            async with self.lock:
                result=await self.operation(connection_id,method,params)
            self.schemas.validate(('v1/' if method=='initialize' else 'v2/')+RPC[method]+'Response.json',result)
            if method in ['turn/start','turn/steer']:
                await self.hit('after-acceptance-before-ack',method,params['threadId'],params.get('clientUserMessageId'))
            if await self.fault('after-acceptance-before-ack',method,socket,request):
                return
            reply={'id':request['id'],'result':result}
            fault=next((f for f in self.reply_faults if f['method']==method),None)
            if fault is not None:
                self.reply_faults.remove(fault)
                self.record('intentional-after-acceptance-reply-fault',request_id=request['id'],method=method,mode=fault['mode'])
                if fault['mode']=='malformed-result':reply={'id':request['id'],'result':{'turnId':None}}
                else:reply={'id':request['id'],'error':{'code':fault['code'],'message':fault['message']}}
            self.schemas.validate('JSONRPCError.json' if 'error' in reply else 'JSONRPCResponse.json',reply)
        except RpcFailure as error:
            reply={'id':request['id'],'error':{'code':error.code,'message':error.message}}
            self.schemas.validate('JSONRPCError.json',reply)
            self.record('native-rejected',method=method,payload=reply)
        except SchemaError as error:
            self.record('schema-failure',message=str(error),payload=request)
            raise
        try:
            await socket.send(json.dumps(reply))
            self.record('server-response',connection_id=connection_id,method=method,payload=reply)
        except ConnectionClosed:
            pass

    async def native_connection(self, socket):
        connection_id = uid('connection')
        self.connections[connection_id]=socket
        self.subscriptions[connection_id]=set()
        handlers=[]
        self.record('connected',connection_id=connection_id)
        try:
            async for raw in socket:
                request=json.loads(raw)
                self.record('client-request',connection_id=connection_id,payload=request)
                handlers.append(asyncio.create_task(self.native_request(socket,connection_id,request)))
        except ConnectionClosed:
            pass
        finally:
            pending=sum(not task.done() for task in handlers)
            for task in handlers:
                if not task.done():
                    task.cancel()
            outcomes=await asyncio.gather(*handlers,return_exceptions=True)
            self.record('handler-cleanup',connection_id=connection_id,pending_cancelled=pending,all_handlers_finished=all(task.done() for task in handlers))
            self.record('disconnected',connection_id=connection_id)
            self.connections.pop(connection_id,None)
            self.subscriptions.pop(connection_id,None)
            for outcome in outcomes:
                if isinstance(outcome,BaseException) and not isinstance(outcome,(asyncio.CancelledError,ConnectionClosed)):
                    self.record('handler-failure',connection_id=connection_id,message=str(outcome))
                    raise outcome

    async def complete_turn(self, thread_id, status='completed', text=None, error=None,message_id=None):
        thread=self.thread(thread_id)
        turn=self.active(thread)
        if turn is None:
            raise RpcFailure(-32600,'fixture no active turn')
        if text is not None:
            turn['items'].append({'type':'agentMessage','id':message_id or uid('item'),'text':text})
        turn['status']=status
        turn['error']=error
        thread['metadata']['status']={'type':'idle'}
        await self.notify('turn/completed',{'threadId':thread_id,'turn':{**clone(turn),'items':[],'itemsView':'summary'}})
        await self.notify('thread/status/changed',{'threadId':thread_id,'status':{'type':'idle'}})
        return {'turnId':turn['id']}

    async def control(self, request):
        op=request['op']
        if op=='barrier-arm':
            key=uid('barrier')
            self.barriers[key]={'point':request['point'],'method':request.get('method'),'thread_id':request.get('thread_id'),'client_message_id':request.get('client_message_id'),'one_shot':request.get('one_shot',False),'hits':0,'released':asyncio.Event()}
            return {'barrier_id':key}
        if op=='barrier-status':
            return {'hits':self.barriers[request['barrier_id']]['hits']}
        if op=='barrier-release':
            self.barriers[request['barrier_id']]['released'].set()
            return True
        if op=='history-items-empty':
            self.empty_item_threads.add(request['thread_id'])
            return True
        if op=='reply-fault-arm':
            assert request.get('message')!='no active turn to steer' and not request.get('message','').startswith('expected active turn id'), 'known rejection cannot be forged after acceptance'
            self.reply_faults.append({'method':request['method'],'mode':request['mode'],'code':request.get('code',-32601),'message':request.get('message','generic unsupported reply')})
            return True
        if op=='fault-arm':
            self.faults.append({'point':request['point'],'method':request['method']})
            return True
        if op=='disconnect':
            await self.connections[request['connection_id']].close(code=1011,reason='fixture controller disconnect')
            return True
        if op=='connections':
            return {'connection_ids':list(self.connections)}
        async with self.lock:
            if op=='duplicate-turn-completion':
                thread=self.thread(request['thread_id'])
                turn=next((turn for turn in thread['turns'] if turn['id']==request['turn_id']),None)
                if turn is None or turn['status'] not in ['completed','failed','interrupted']:
                    raise RpcFailure(-32600,'fixture duplicate completion requires exact ended turn')
                await self.notify('turn/completed',{'threadId':request['thread_id'],'turn':{**clone(turn),'items':[],'itemsView':'summary'}})
                return True
            if op=='complete-turn':
                return await self.complete_turn(request['thread_id'],request.get('status','completed'),request.get('text'),request.get('error'),request.get('message_id'))
            if op=='spawn-child':
                child=self.create_thread(parent=request['thread_id'])
                child_id=child['metadata']['id']
                parent=self.thread(request['thread_id'])
                active=self.active(parent)
                if active:
                    activity={'type':'subAgentActivity','id':uid('item'),'agentPath':'/fixture/child','agentThreadId':child_id,'kind':'started'}
                    active['items'].append(activity)
                    await self.notify('item/completed',{'threadId':request['thread_id'],'turnId':active['id'],'item':activity,'completedAtMs':int(time.time()*1000)})
                await self.notify('thread/started',{'thread':self.metadata(child)})
                return {'thread_id':child_id}
            if op=='command-start':
                thread=self.thread(request['thread_id'])
                turn=self.active(thread)
                if turn is None:
                    raise RpcFailure(-32600,'fixture command requires active owning turn')
                item={'type':'commandExecution','id':uid('item'),'command':request.get('command','fixture command'),
                    'commandActions':[],'cwd':thread['metadata']['cwd'],'source':'unifiedExecStartup',
                    'processId':uid('process'),'status':'inProgress','exitCode':None,'aggregatedOutput':None,'durationMs':None}
                turn['items'].append(item)
                thread['terminals'][item['id']]={k:item[k] for k in ['command','cwd','processId']}
                thread['terminals'][item['id']]['itemId']=item['id']
                await self.notify('item/started',{'threadId':request['thread_id'],'turnId':turn['id'],'item':item,'startedAtMs':int(time.time()*1000)})
                return {'item_id':item['id'],'turn_id':turn['id']}
            if op=='commands-complete-many':
                completed=[]
                for value in request['commands']:
                    thread=self.thread(value['thread_id'])
                    for turn in thread['turns']:
                        for item in turn['items']:
                            if item['id']==value['item_id']:
                                item.update(status=value.get('status','completed'),exitCode=value.get('exit_code'),aggregatedOutput=value.get('output'),durationMs=value.get('duration_ms'))
                                thread['terminals'].pop(item['id'],None)
                                completed.append((value['thread_id'],turn['id'],clone(item)))
                assert len(completed)==len(request['commands'])
                for thread_id,turn_id,item in completed:
                    await self.notify('item/completed',{'threadId':thread_id,'turnId':turn_id,'item':item,'completedAtMs':int(time.time()*1000)})
                return True
            if op=='command-complete':
                thread=self.thread(request['thread_id'])
                for turn in thread['turns']:
                    for item in turn['items']:
                        if item['id']==request['item_id']:
                            item.update(status=request.get('status','completed'),exitCode=request.get('exit_code'),aggregatedOutput=request.get('output'),durationMs=request.get('duration_ms'))
                            thread['terminals'].pop(item['id'],None)
                            await self.notify('item/completed',{'threadId':request['thread_id'],'turnId':turn['id'],'item':item,'completedAtMs':int(time.time()*1000)})
                            return True
                raise RpcFailure(-32600,'fixture item not found')
            if op=='duplicate-item-completion':
                thread=self.thread(request['thread_id'])
                for turn in thread['turns']:
                    for item in turn['items']:
                        if item['id']==request['item_id']:
                            await self.notify('item/completed',{'threadId':request['thread_id'],'turnId':turn['id'],'item':item,'completedAtMs':int(time.time()*1000)})
                            return True
                raise RpcFailure(-32600,'fixture item not found')
            if op=='terminal-interaction':
                await self.notify('item/commandExecution/terminalInteraction',request['params'])
                return True
            if op=='status':
                thread=self.thread(request['thread_id'])
                thread['metadata']['status']=request['status']
                await self.notify('thread/status/changed',{'threadId':request['thread_id'],'status':request['status']})
                return True
        raise ValueError('unsupported fixture control op: '+op)

    async def control_connection(self, socket):
        async for raw in socket:
            request=json.loads(raw)
            self.record('fixture-control',payload=request)
            try:
                result=await self.control(request)
                await socket.send(json.dumps({'result':result}))
            except (RpcFailure,ValueError,KeyError) as error:
                await socket.send(json.dumps({'fixture_error':str(error)}))

    async def serve(self):
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        os.chmod(self.directory,0o700)
        for name in ['home','codex-home','workspace']:
            (self.directory/name).mkdir(mode=0o700,exist_ok=True)
        self.sockets=[self.native_socket,self.directory/'control.sock']
        async with unix_serve(self.native_connection,str(self.sockets[0])), unix_serve(self.control_connection,str(self.sockets[1])):
            for socket in self.sockets:
                os.chmod(socket,0o600)
            (self.directory/'ready.json').write_text(json.dumps({'pid':os.getpid(),'native_socket':str(self.sockets[0]),'control_socket':str(self.sockets[1])}))
            self.ready.set()
            await self.stopped.wait()
        for socket in self.sockets:
            socket.unlink(missing_ok=True)
        (self.directory/'validation-counts.json').write_text(json.dumps(self.schemas.counts,indent=2))


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--schemas',default=SCHEMAS)
    parser.add_argument('--native-socket')
    args=parser.parse_args()
    provider=Provider(args.run_dir,args.schemas,args.native_socket)
    loop=asyncio.get_running_loop()
    for sig in [signal.SIGINT,signal.SIGTERM]:
        loop.add_signal_handler(sig,provider.stopped.set)
    await provider.serve()

if __name__=='__main__':
    asyncio.run(main())
