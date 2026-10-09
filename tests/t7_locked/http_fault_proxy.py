"""Own Unix HTTP transport fault proxy to the real listener; no product imports."""
import asyncio
import json
from pathlib import Path

async def message(reader):
    header=await reader.readuntil(b'\r\n\r\n');lines=header.split(b'\r\n');length=None
    for line in lines[1:]:
        key,separator,value=line.partition(b':')
        if separator and key.lower()==b'content-length':length=int(value.strip())
    return header,await reader.read() if length is None else await reader.readexactly(length)

class HTTPFaultProxy:
    def __init__(self,path,upstream,evidence,config=None):
        self.path=Path(path);self.upstream=str(upstream);self.evidence=Path(evidence);self.rules=[];self.connections=set();self.server=None
        self.config=Path(config) if config else None
    async def start(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.server=await asyncio.start_unix_server(self.handle,path=str(self.path));self.path.chmod(0o600);return self
    def arm(self,event_type=None,route=None,mode='drop-response'):
        rule={'event_type':event_type,'route':route,'mode':mode,'hits':0};self.rules.append(rule);return rule
    def record(self,**value):
        self.evidence.parent.mkdir(parents=True,exist_ok=True)
        with self.evidence.open('a') as file:file.write(json.dumps(value)+'\n')
    async def handle(self,reader,writer):
        task=asyncio.current_task();self.connections.add(task);upstream_writer=None
        try:
            if self.config:
                seen={rule['id'] for rule in self.rules}
                for rule in json.loads(self.config.read_text()):
                    if rule['id'] not in seen:self.rules.append({**rule,'hits':0})
            header,body=await message(reader);route=header.split(b' ',2)[1].decode();request=json.loads(body);event_type=request.get('event',{}).get('type')
            upstream_reader,upstream_writer=await asyncio.open_unix_connection(self.upstream);upstream_writer.write(header+body);await upstream_writer.drain();response_header,response_body=await message(upstream_reader)
            rule=next((r for r in self.rules if not r['hits'] and r['event_type'] in [None,event_type] and r['route'] in [None,route]),None)
            self.record(route=route,event_type=event_type,request=request,upstream_response=response_body.decode(),fault=rule['mode'] if rule else None)
            if rule:
                rule['hits']+=1
                if self.config:self.config.with_suffix('.hits.json').write_text(json.dumps([r['id'] for r in self.rules if r['hits']]))
                if rule['mode']=='drop-response':return
                replacement=b'null' if rule['mode']=='null-view' else b'{malformed-json'
                response_header=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(replacement)).encode()+b'\r\nConnection: close\r\n\r\n';response_body=replacement
            writer.write(response_header+response_body);await writer.drain()
        except (ConnectionError,asyncio.IncompleteReadError):pass
        finally:
            if upstream_writer:upstream_writer.close();await upstream_writer.wait_closed()
            writer.close();await writer.wait_closed();self.connections.discard(task)
    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        for task in list(self.connections):task.cancel()
        await asyncio.gather(*list(self.connections),return_exceptions=True)
        assert not self.connections,'OWN_HTTP_PROXY_HANDLERS_SURVIVE';self.path.unlink(missing_ok=True)
        self.record(cleanup=True,all_proxy_handlers_finished=True,socket_absent=not self.path.exists())

async def main():
    import argparse
    import signal
    parser=argparse.ArgumentParser();parser.add_argument('--socket',required=True);parser.add_argument('--upstream',required=True);parser.add_argument('--config',required=True);parser.add_argument('--evidence',required=True);args=parser.parse_args()
    proxy=HTTPFaultProxy(args.socket,args.upstream,args.evidence,config=args.config)
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in [signal.SIGTERM,signal.SIGINT]:loop.add_signal_handler(sig,stop.set)
    await proxy.start()
    try:await stop.wait()
    finally:await proxy.close()

if __name__=='__main__':asyncio.run(main())
