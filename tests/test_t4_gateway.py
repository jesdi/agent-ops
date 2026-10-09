"""Gateway receipt behavior through the executable supervisor and native RPC."""
import asyncio
import json

from websockets.asyncio.client import unix_connect

from dispatcher.runtime_http import RuntimeClient
from tests.test_codex_supervisor import running_main, isolated, eventually  # noqa: F401
from tests.test_t4_runtime_acceptance import world, eventually as external_eventually  # noqa: F401


def test_gateway_correlates_acceptance_rejection_and_duplicate_input(isolated, monkeypatch):
    with running_main(isolated, monkeypatch) as (running, future):
        root = running.ready()
        client = RuntimeClient(running.state)
        binding = running.snapshot()['binding']
        running.command('stop', threadId=root)
        eventually(lambda: running.snapshot()['main']['status'] == 'stopped', 'bootstrap completes')
        endpoint = running.records('terminal-started')[0]['endpoint'].removeprefix('unix://')

        async def inputs():
            async with unix_connect(path=endpoint) as socket:
                async def call(identity, method, **params):
                    await socket.send(json.dumps(dict(id=identity, method=method, params=params)))
                    return json.loads(await asyncio.wait_for(socket.recv(), 3))
                await call('init', 'initialize', clientInfo={'name': 'operator-fixture'})
                rejected = await call('steer', 'turn/steer', threadId=root,
                                      expectedTurnId='finished', clientUserMessageId='rejected', input=[])
                assert rejected['error']['code'] == -32601
                accepted = await call('start', 'turn/start', threadId=root,
                                      clientUserMessageId='accepted', input=[{'type': 'text', 'text': 'next'}])
                assert accepted['result']['turn']['status'] == 'inProgress'
                duplicate = await call('duplicate', 'turn/start', threadId=root,
                                       clientUserMessageId='accepted', input=[])
                assert 'error' in duplicate

        asyncio.run(inputs())
        running.command('stop', threadId=root)
        eventually(lambda: running.snapshot()['main']['status'] == 'stopped', 'operator turn completes')
        assert client.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
        assert client.retire(binding, running.snapshot()['revision']) == 'retired'
        forwarded = [row for row in running.records('rpc')
                     if row['method'] == 'turn/start' and row['params'].get('clientUserMessageId') == 'accepted']
        assert len(forwarded) == 1
        assert not future.done()


def test_terminal_cannot_fork_or_override_its_resume_selector(isolated, monkeypatch):
    with running_main(isolated, monkeypatch) as (running, future):
        root = running.ready()
        endpoint = running.records('terminal-started')[0]['endpoint'].removeprefix('unix://')

        async def selectors():
            async with unix_connect(path=endpoint) as socket:
                cases = [
                    ('fork', 'thread/fork', {'threadId': root}),
                    ('history', 'thread/resume', {'threadId': root, 'history': [{'type': 'message', 'role': 'user', 'content': []}]}),
                    ('path', 'thread/resume', {'threadId': root, 'path': '/another/rollout.jsonl'}),
                ]
                for identity, method, params in cases:
                    await socket.send(json.dumps(dict(id=identity, method=method, params=params)))
                    response = json.loads(await asyncio.wait_for(socket.recv(), 3))
                    assert 'error' in response
                for identity, method, params in [
                    ('root', 'thread/resume', {'threadId': root}),
                    ('nulls', 'thread/resume', {'threadId': root, 'path': None, 'history': None}),
                    ('empty-path', 'thread/resume', {'threadId': root, 'path': ''}),
                    ('browse', 'thread/read', {'threadId': 'foreign-newest'}),
                ]:
                    await socket.send(json.dumps(dict(id=identity, method=method, params=params)))
                    assert 'result' in json.loads(await asyncio.wait_for(socket.recv(), 3))

        asyncio.run(selectors())
        assert not any(row['method'] == 'thread/fork' for row in running.records('rpc'))
        assert not any(row['method'] == 'thread/resume' and (row['params'].get('history') is not None or bool(row['params'].get('path')))
                       for row in running.records('rpc'))
        assert not future.done()


def test_retired_fresh_supervisor_exits_before_starting_any_backend(world):
    binding = world.prepare(conversation=None)
    before = world.view(binding)
    assert world.host.retire(binding, before['revision'], reason='forced') == 'retired'
    retired = world.view(binding)
    process = world.supervisor(binding, ready=False)
    external_eventually(lambda: process.poll() is not None, 'retired fresh launch must exit promptly')
    assert process.returncode != 0
    assert world.logs() == [], 'retired launch must not create a backend or conversation'
    assert world.view(binding) == retired
