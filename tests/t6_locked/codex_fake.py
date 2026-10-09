"""Own external codex ARGV process adapter; never invokes a native CLI/model."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from websockets.asyncio.server import unix_serve
from native_client import Client
from provider import Provider

async def terminal(socket_path,root,run):
    client=await Client().open(socket_path)
    await client.rpc('initialize',{'clientInfo':{'name':'own-terminal','version':'1'},'capabilities':{'experimentalApi':True}})
    await client.socket.send(json.dumps({'method':'initialized'}))
    await client.rpc('thread/resume',{'threadId':root})
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in [signal.SIGINT,signal.SIGTERM]:loop.add_signal_handler(sig,stop.set)
    path=run/'terminal.sock'
    async def commands(socket):
        async for raw in socket:
            command=json.loads(raw)
            if command['op']=='input':
                response=await client.rpc('turn/start',{'threadId':root,'input':[{'type':'text','text':command['text']}],'clientUserMessageId':command.get('client_message_id')})
            elif command['op']=='read':response=await client.rpc('thread/read',{'threadId':root,'includeTurns':True})
            else:raise ValueError('undeclared terminal fixture operation')
            await socket.send(json.dumps(response))
    try:
        async with unix_serve(commands,str(path)):
            os.chmod(path,0o600)
            (run/'terminal-ready.json').write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'root':root,'gateway':str(socket_path)}))
            await stop.wait()
    finally:
        await client.close();path.unlink(missing_ok=True)
        (run/'terminal-cleanup.json').write_text(json.dumps({'pid':os.getpid(),'socket_removed':not path.exists()}))

async def main():
    args=sys.argv[1:];run=Path(os.environ['T6_FIXTURE_RUN_DIR']);run.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (run/'argv.jsonl').open('a') as handle:handle.write(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'args':args})+'\n')
    if len(args)>=3 and args[:2]==['app-server','--listen'] and args[2].startswith('unix://'):
        overrides=args[3:]
        assert len(overrides)%2==0 and all(overrides[index]=='-c' and '=' in overrides[index+1] for index in range(0,len(overrides),2)),'undeclared config override shape'
        provider=Provider(run,native_socket=args[2][7:]);loop=asyncio.get_running_loop()
        for sig in [signal.SIGINT,signal.SIGTERM]:loop.add_signal_handler(sig,provider.stopped.set)
        await provider.serve()
    elif len(args)==7 and args[0]=='--remote' and args[2]=='-C' and args[4:6]==['resume','--'] and args[1].startswith('unix://'):
        await terminal(Path(args[1][7:]),args[6],run)
    else:raise ValueError('undeclared codex process ARGV: '+repr(args))

if __name__=='__main__':asyncio.run(main())
