"""Own correlated JSON-RPC client used only for external native fixture traffic."""
import asyncio
import json
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

class Client:
    async def open(self,path):
        self.socket=await unix_connect(str(path));self.next_id=0;self.waiters={};self.notifications=[]
        self.reader=asyncio.create_task(self.read());return self
    async def read(self):
        try:
            async for raw in self.socket:
                packet=json.loads(raw)
                if 'id' in packet:
                    waiter=self.waiters.pop(packet['id'],None)
                    if waiter is not None and not waiter.done():waiter.set_result(packet)
                else:self.notifications.append(packet)
        except ConnectionClosed:pass
        finally:
            for waiter in self.waiters.values():
                if not waiter.done():waiter.set_exception(ConnectionError('owned native channel disconnected'))
            self.waiters.clear()
    async def rpc(self,method,params,timeout=8):
        self.next_id+=1;request_id=self.next_id;waiter=asyncio.get_running_loop().create_future();self.waiters[request_id]=waiter
        await self.socket.send(json.dumps({'id':request_id,'method':method,'params':params}))
        return await asyncio.wait_for(waiter,timeout)
    async def close(self):
        await self.socket.close();await self.reader
