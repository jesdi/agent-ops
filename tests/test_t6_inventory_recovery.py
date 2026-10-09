"""Inventory reply failure/recovery through the live supervisor and wire boundary."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from t6_locked.external_fixture import ARTIFACTS, ExternalFixture, eventually
from test_t6_review_recovery import polls


@pytest.mark.parametrize('result', [None, [], 'unreadable'], ids=['null', 'list', 'scalar'])
def test_inventory_read_malformed_reply_preserves_uncertainty_and_progress(result):
    asyncio.run(inventory_recovery('read', result))


@pytest.mark.parametrize('result', [None, [], 'unreadable'], ids=['null', 'list', 'scalar'])
def test_retained_owner_malformed_read_preserves_uncertainty_and_progress(result):
    asyncio.run(inventory_recovery('retained', result))


@pytest.mark.parametrize('result', [None, [], 'unreadable'], ids=['null', 'list', 'scalar'])
def test_not_loaded_resume_malformed_reply_preserves_uncertainty_and_progress(result):
    asyncio.run(inventory_recovery('resume', result))


async def inventory_recovery(mode, result):
    flow = ExternalFixture(name='inventory-' + mode + '-' + type(result).__name__)
    flow.env['PYTHONPYCACHEPREFIX'] = os.environ.get('PYTHONPYCACHEPREFIX', str(flow.directory / 'bytecode'))
    wrapper = flow.directory / 'bin/codex'
    helper = Path(__file__).with_name('t6_inventory_wire.py')
    wrapper.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path('
        + repr(str(helper)) + ',run_name="__main__")\n')
    try:
        await flow.start()
        first = await flow.command('first owned result')
        second = await flow.command('later owned result')
        await flow.qualify([first, second])
        owner = flow.root
        if mode == 'retained':
            owner = (await flow.control('spawn-child', thread_id=flow.root))['thread_id']
            await eventually(lambda: any(row['kind'] == 'client-request'
                and row['payload']['method'] == 'thread/backgroundTerminals/list'
                and row['payload']['params']['threadId'] == owner for row in flow.records()),
                'SETUP_CHILD_OWNER_NOT_OBSERVED')
            await eventually(lambda: flow.view()['inventory'] == 'known', 'SETUP_CHILD_SCAN_NOT_COMPLETE')
            await eventually(lambda: any(scope['thread_id'] == owner
                for scope in (flow.view()['history_checkpoint'] or {}).get('scopes', [])),
                'SETUP_CHILD_HISTORY_NOT_PUBLISHED')
        assert flow.host.accept_input(flow.binding, 'unresolved-before-fault')
        before = flow.view()
        assert before['inventory'] == 'known'
        assert before['main']['status'] == 'stopped'
        assert before['wait'] is not None
        (flow.run / 'inventory-fault.json').write_text(json.dumps({
            'mode': mode, 'owner': owner, 'result': result}))
        await eventually(lambda: (flow.run / 'inventory-fault-reply.json').exists(),
            'SETUP_INVENTORY_WIRE_FAULT_NOT_EXERCISED')
        await eventually(lambda: flow.view()['inventory'] == 'unknown' and any(
            a['kind'] == 'compatibility' for a in flow.view()['alerts']),
            'MALFORMED_INVENTORY_REPLY_LEFT_STALE_KNOWN_STATE')
        await eventually(lambda: (flow.run / 'inventory-next-poll.json').exists(),
            'MALFORMED_INVENTORY_REPLY_STOPPED_OBSERVATION')
        held = flow.view()
        assert held['service'] == 'live'
        assert flow.supervisor.poll() is None
        assert held['inputs'] == before['inputs']
        assert held['history_checkpoint'] == before['history_checkpoint']
        assert held['completions'] == before['completions'] == []
        assert held['deliveries'] == before['deliveries'] == []
        assert held['wait'] == before['wait']
        assert flow.host.retire(flow.binding, held['revision'], reason='stopped') == 'held'
        assert flow.result_requests() == []
        operator = await flow.terminal('input', text='continue while inventory is unknown',
            client_message_id='inventory-operator')
        assert 'result' in operator, operator
        count = polls(flow)
        (flow.run / 'inventory-release').touch()
        await eventually(lambda: polls(flow) >= count + 2,
            'INVENTORY_POLLING_DID_NOT_RECOVER')
        await eventually(lambda: flow.view()['inventory'] == 'known',
            'READABLE_INVENTORY_DID_NOT_RESTORE_KNOWN_STATE')
        first_identity = await flow.finish(first, status='failed', exit_code=7, output='known failure', duration=0)
        await eventually(lambda: len(flow.view()['deliveries']) == 1
            and flow.view()['deliveries'][0]['status'] == 'confirmed',
            'FIRST_OWNED_OUTCOME_DID_NOT_REACH_MAIN')
        second_identity = await flow.finish(second, output='', duration=None)
        await eventually(lambda: len(flow.view()['deliveries']) == 2
            and all(b['status'] == 'confirmed' for b in flow.view()['deliveries']),
            'LATER_OWNED_OUTCOME_DID_NOT_REACH_MAIN')
        after = flow.view()
        assert after['inputs']['unresolved-before-fault'] == before['inputs']['unresolved-before-fault']
        assert after['wait']['since'] == before['wait']['since']
        assert after['wait']['ever_reported'] == before['wait']['ever_reported']
        requests = flow.result_requests()
        assert len(requests) == 2
        assert [b['completion_ids'] for b in after['deliveries']] == [[first_identity], [second_identity]]
        assert all(len(b['attempts']) == 1 for b in after['deliveries'])
        assert all(packet['method'] == 'turn/steer' and packet['params']['threadId'] == flow.root
            and packet['params']['expectedTurnId'] == after['main']['turn_id'] for packet, _ in requests)
        assert [records[0]['outcome'] for _, records in requests] == [
            {'status': 'failed', 'exit_code': 7, 'aggregated_output': 'known failure', 'duration_ms': 0},
            {'status': 'completed', 'exit_code': 0, 'aggregated_output': '', 'duration_ms': None}]
        (ARTIFACTS / (flow.name + '-evidence.json')).write_text(json.dumps({
            'before': before, 'held': held, 'after': after, 'operator': operator,
            'polls_before_recovery': count, 'polls_after': polls(flow), 'result_requests': requests}, indent=2))
    finally:
        for path in flow.run.glob('inventory-*.json'):
            (ARTIFACTS / (flow.name + '-' + path.name)).write_bytes(path.read_bytes())
        await flow.close()
