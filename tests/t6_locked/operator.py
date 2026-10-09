#!/usr/bin/env python3
"""Independent interactive terminal client for this isolated native-schema fake."""
import argparse
import asyncio
import json
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--socket',required=True)
    parser.add_argument('--thread-id',required=True)
    args=parser.parse_args()
    async with unix_connect(args.socket) as socket:
        next_id=0
        pending={}
        async def reader():
            try:
                async for raw in socket:
                    packet=json.loads(raw)
                    if 'id' in packet and packet['id'] in pending:
                        pending.pop(packet['id']).set_result(packet)
                    else:
                        print('\n'+json.dumps(packet,sort_keys=True),flush=True)
            finally:
                for waiter in pending.values():
                    if not waiter.done():waiter.set_exception(ConnectionError('operator channel closed'))
        reading=asyncio.create_task(reader())
        async def rpc(method,params):
            nonlocal next_id
            next_id+=1
            waiter=asyncio.get_running_loop().create_future()
            pending[next_id]=waiter
            await socket.send(json.dumps({'id':next_id,'method':method,'params':params}))
            return await waiter
        await rpc('initialize',{'clientInfo':{'name':'fixture-operator','version':'1'},'capabilities':{'experimentalApi':True}})
        await socket.send(json.dumps({'method':'initialized'}))
        await rpc('thread/resume',{'threadId':args.thread_id})
        print('Fixture operator attached. Enter text; /read reads current exact thread; /quit exits.',flush=True)
        try:
            while True:
                text=await asyncio.to_thread(input,'operator> ')
                if text=='/quit':break
                if text=='/read':
                    print(json.dumps(await rpc('thread/read',{'threadId':args.thread_id,'includeTurns':True}),indent=2),flush=True)
                    continue
                # Native turn/start steers an already active same-thread turn.
                receipt=await rpc('turn/start',{'threadId':args.thread_id,'input':[{'type':'text','text':text}]})
                print(json.dumps(receipt),flush=True)
        except (EOFError,ConnectionClosed,ConnectionError):
            pass
        finally:
            await socket.close()
            await reading

if __name__=='__main__':
    asyncio.run(main())
