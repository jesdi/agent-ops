#!/usr/bin/env python3
"""Own native-schema fixture extensions; T7 preparation only, no product imports."""
import argparse
import asyncio
import json
import signal
from native_base import Provider as RetainedProvider, RpcFailure, clone, uid
from websockets.exceptions import ConnectionClosed


class Provider(RetainedProvider):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.root_reply_barriers={}
        self.notification_drops=[]
        self.history_faults={}
        self.control_connections={}
        self.hide_active_receipts=False
        self.page_size_cap=None

    def page(self,method,params,data,default_direction='asc'):
        if self.page_size_cap is not None:params={**params,'limit':min(params.get('limit') or self.page_size_cap,self.page_size_cap)}
        return super().page(method,params,data,default_direction)

    def connection_id(self,socket):
        return next(key for key,value in self.connections.items() if value is socket)

    @staticmethod
    def matches(rule,method,params,connection_id=None,request_id=None):
        selectors={'method':method,'thread_id':params.get('threadId'),'turn_id':params.get('turnId'),
                   'sort_direction':params.get('sortDirection') or 'asc','client_message_id':params.get('clientUserMessageId'),
                   'connection_id':connection_id,'request_id':request_id}
        return all(rule.get(key) is None or rule[key]==value for key,value in selectors.items())

    async def fault(self,point,method,socket,request):
        connection_id=self.connection_id(socket);params=request.get('params',{})
        if method in ['thread/start','thread/resume']:
            for key,barrier in list(self.root_reply_barriers.items()):
                if point!=barrier.get('point','after-acceptance-before-ack') or barrier['hits'] or not self.matches(barrier,method,params,connection_id,request['id']):continue
                barrier['hits']=1
                self.record('root-reply-barrier-hit',barrier_id=key,connection_id=connection_id,request_id=request['id'],method=method)
                await barrier['released'].wait()
        match=next((value for value in self.faults if value['point']==point and self.matches(value,method,params,connection_id,request['id'])),None)
        if match is None:return False
        self.faults.remove(match)
        self.record('fault',point=point,method=method,connection_id=connection_id,request_id=request['id'],
                    mode=match.get('mode','disconnect'),certainty='proven-nonacceptance' if point=='before-native-operation' else 'uncertain-acceptance')
        if match.get('mode','disconnect')=='disconnect':await socket.close(code=1011,reason='owned connection-specific fixture fault')
        return True

    async def notify(self,method,params):
        payload={'method':method,'params':clone(params)}
        self.schemas.validate('ServerNotification.json',payload);self.schemas.validate('JSONRPCNotification.json',payload)
        thread_id=params.get('threadId',params.get('thread',{}).get('id'))
        for connection_id,socket in list(self.connections.items()):
            if thread_id not in self.subscriptions.get(connection_id,set()):continue
            rule=next((rule for rule in self.notification_drops if rule['connection_id']==connection_id and rule['method']==method and rule.get('thread_id') in [None,thread_id]),None)
            if rule is not None:
                self.record('server-notification-dropped',connection_id=connection_id,payload=payload)
                if rule.get('one_shot',True):self.notification_drops.remove(rule)
                continue
            try:
                await socket.send(json.dumps(payload));self.record('server-notification',connection_id=connection_id,payload=payload)
            except ConnectionClosed:pass

    @staticmethod
    def normalize_item(item):
        if item.get('type')=='userMessage':
            for part in item['content']:
                if part.get('type')=='text' and 'text_elements' not in part:part['text_elements']=[]

    def metadata(self,thread,hydrate=False):
        result=super().metadata(thread,hydrate)
        for turn in result['turns']:
            if turn['status']!='inProgress':
                for item in turn['items']:self.normalize_item(item)
            elif self.hide_active_receipts:
                turn['items']=[item for item in turn['items'] if item['type']!='userMessage'];turn['itemsView']='summary'
        return result

    async def operation(self,connection_id,method,params):
        rules=[rule for rule in self.history_faults.values() if self.matches(rule,method,params,connection_id)]
        if any(rule['mode']=='error' for rule in rules):
            self.record('history-observation-fault',connection_id=connection_id,method=method,params=params,modes=[r['mode'] for r in rules],error='fixture scoped history unreadable')
            raise RpcFailure(-32600,'fixture scoped history unreadable')
        result=await super().operation(connection_id,method,params)
        if method=='thread/items/list':
            for entry in result['data']:self.normalize_item(entry['item'])
            if self.hide_active_receipts:
                active=self.active(self.thread(params['threadId']))
                if active:result['data']=[entry for entry in result['data'] if entry['turnId']!=active['id'] or entry['item']['type']!='userMessage']
        elif method=='thread/turns/list':
            for turn in result['data']:
                if turn['status']!='inProgress':
                    for item in turn['items']:self.normalize_item(item)
                elif self.hide_active_receipts:
                    turn['items']=[item for item in turn['items'] if item['type']!='userMessage'];turn['itemsView']='summary'
        for rule in rules:
            mode=rule['mode']
            if mode=='repeat-cursor' and rule.get('candidate_client_message_id') is not None and not params.get('cursor'):
                entries=[{'turnId':turn['id'],'item':clone(item)} for turn in self.thread(params['threadId'])['turns'] if turn['status']!='inProgress' and (not params.get('turnId') or params['turnId']==turn['id']) for item in turn['items'] if item.get('clientId')==rule['candidate_client_message_id']]
                assert len(entries)==1,'fixture requires one actual exact receipt candidate'
                self.normalize_item(entries[0]['item'])
                result['data']=[entry for entry in result['data'] if entry['item']['id']!=entries[0]['item']['id']][:1]+entries
            if mode=='repeat-cursor' and params.get('cursor'):result['nextCursor']=params['cursor']
            elif mode=='missing-next-cursor':
                candidate=rule.get('candidate_client_message_id')
                if candidate is not None and not params.get('cursor'):
                    entries=[{'turnId':turn['id'],'item':clone(item)} for turn in self.thread(params['threadId'])['turns'] if turn['status']!='inProgress' and (not params.get('turnId') or params['turnId']==turn['id']) for item in turn['items'] if item.get('clientId')==candidate]
                    assert len(entries)==1,'fixture requires one actual exact receipt candidate'
                    self.normalize_item(entries[0]['item'])
                    result['data']=[entry for entry in result['data'] if entry['item']['id']!=entries[0]['item']['id']][:1]+entries
                result.pop('nextCursor',None)
            elif mode=='summary-turn':
                turns=result['thread']['turns'] if method=='thread/read' else result['data']
                for turn in turns:
                    if turn['id']!=rule['summary_turn_id'] or turn.get('itemsView')!='full':continue
                    assert turn['status']=='completed' and turn['error'] is None
                    assert any(item.get('clientId')==rule['retain_client_message_id'] for item in turn['items'])
                    assert any(item.get('clientId')==rule['omit_client_message_id'] for item in turn['items'])
                    turn['items']=[item for item in turn['items'] if item.get('clientId')!=rule['omit_client_message_id']];turn['itemsView']='summary'
            elif mode=='foreign-thread':result['thread']['id']='opaque-foreign-returned-thread'
            elif mode=='duplicate-page':
                if params.get('cursor') and 'previous_data' in rule:result['data']=clone(rule['previous_data'])
                else:rule['previous_data']=clone(result['data'])
            elif mode=='mismatched-turn':
                for entry in result['data']:
                    if entry['item']['type']=='userMessage':entry['turnId']='opaque-foreign-receipt-turn'
            elif mode in ['mismatched-text','missing-client','null-client']:
                for entry in result['data']:
                    item=entry['item']
                    if item['type']!='userMessage':continue
                    if mode=='missing-client':item.pop('clientId',None)
                    elif mode=='null-client':item['clientId']=None
                    else:
                        for part in item['content']:
                            if part['type']=='text':part['text']+=' generic changed receipt'
        if rules:self.record('history-observation-fault',connection_id=connection_id,method=method,params=params,modes=[r['mode'] for r in rules],result=result)
        return result

    async def control(self,request):
        op=request['op']
        if op=='persistent-malformed-items':
            if request['enabled']:
                self.reply_faults.append({'method':'thread/items/list','mode':'malformed-history','persistent':True})
            else:self.reply_faults=[fault for fault in self.reply_faults if not fault.get('persistent',False)]
            return True
        if op=='page-size-cap':
            assert isinstance(request['limit'],int) and not isinstance(request['limit'],bool) and request['limit']>0
            self.page_size_cap=request['limit'];self.record('explicit-page-size-cap',limit=self.page_size_cap);return True
        if op=='hide-active-receipts':
            self.hide_active_receipts=bool(request['enabled']);self.record('explicit-active-receipt-visibility-fault',enabled=self.hide_active_receipts);return True
        if op=='scoped-fault-arm':
            assert request['mode'] in ['disconnect','drop-reply']
            assert request['point'] in ['before-native-operation','after-acceptance-before-ack']
            self.faults.append({key:request.get(key) for key in ['point','method','mode','connection_id','request_id','client_message_id','thread_id']})
            return True
        if op=='root-reply-barrier-arm':
            assert request['method'] in ['thread/start','thread/resume']
            assert request.get('point','after-acceptance-before-ack') in ['before-native-operation','after-acceptance-before-ack']
            key=uid('root-barrier');self.root_reply_barriers[key]={**request,'hits':0,'released':asyncio.Event()};return {'barrier_id':key}
        if op=='root-reply-barrier-status':return {'hits':self.root_reply_barriers[request['barrier_id']]['hits']}
        if op=='root-reply-barrier-release':self.root_reply_barriers[request['barrier_id']]['released'].set();return True
        if op=='notification-drop-arm':
            assert request['connection_id'] in self.connections
            self.notification_drops.append({**{key:request.get(key) for key in ['method','connection_id','thread_id']},'one_shot':request.get('one_shot',True)});return True
        if op=='summary-turn-arm':
            assert request['method'] in ['thread/read','thread/turns/list']
            assert all(isinstance(request.get(key),str) and request[key] for key in ['thread_id','summary_turn_id','retain_client_message_id','omit_client_message_id'])
            key=uid('history-fault');self.history_faults[key]={**clone(request),'mode':'summary-turn'};return {'fault_id':key}
        if op=='history-fault-arm':
            assert request['method'] in ['thread/items/list','thread/turns/list','thread/read']
            assert request['mode'] in ['repeat-cursor','missing-next-cursor','duplicate-page','error','mismatched-text','mismatched-turn','missing-client','null-client','foreign-thread']
            if request['method']=='thread/read':assert request['mode'] in ['error','foreign-thread']
            if request['mode']=='foreign-thread':assert request['method']=='thread/read'
            if request['mode'] in ['mismatched-text','mismatched-turn','missing-client','null-client']:assert request['method']=='thread/items/list'
            key=uid('history-fault');self.history_faults[key]=clone(request);return {'fault_id':key}
        if op=='history-fault-clear':self.history_faults.pop(request['fault_id']);return True
        if op=='control-connections':return {'connection_ids':list(self.control_connections)}
        return await super().control(request)

    async def control_connection(self,socket):
        key=uid('control');self.control_connections[key]=socket;self.record('control-connected',connection_id=key)
        try:
            async for raw in socket:
                request=json.loads(raw);self.record('fixture-control',connection_id=key,payload=request)
                try:await socket.send(json.dumps({'result':await self.control(request)}))
                except (RpcFailure,ValueError,KeyError) as error:await socket.send(json.dumps({'fixture_error':str(error)}))
        except ConnectionClosed:pass
        finally:self.control_connections.pop(key,None);self.record('control-disconnected',connection_id=key)


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-dir',required=True);parser.add_argument('--schemas');parser.add_argument('--native-socket');args=parser.parse_args()
    options={'native_socket':args.native_socket}
    if args.schemas:options['schemas']=args.schemas
    provider=Provider(args.run_dir,**options);loop=asyncio.get_running_loop()
    for sig in [signal.SIGINT,signal.SIGTERM]:loop.add_signal_handler(sig,provider.stopped.set)
    await provider.serve()

if __name__=='__main__':asyncio.run(main())
