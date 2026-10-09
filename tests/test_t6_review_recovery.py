"""Malformed native success replies must not strand a live bound session."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

from t6_locked.external_fixture import ARTIFACTS, ExternalFixture, eventually


def polls(flow):
    return sum(row['kind'] == 'client-request'
        and row['payload']['method'] == 'thread/backgroundTerminals/list'
        for row in flow.records())


@pytest.mark.parametrize('result', [None, [], 'unreadable'], ids=['null', 'list', 'scalar'])
def test_malformed_root_result_reports_problem_and_recovers_delivery(result):
    asyncio.run(root_recovery(result))


async def root_recovery(result):
    name = 'review-root-' + type(result).__name__
    flow = ExternalFixture(name=name)
    wrapper = flow.directory / 'bin/codex'
    helper = Path(__file__).with_name('t6_review_wire.py')
    wrapper.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path('
        + repr(str(helper)) + ',run_name="__main__")\n')
    try:
        await flow.start()
        first = await flow.command('first result')
        later = await flow.command('later result')
        await flow.qualify([first, later])
        # An independent unresolved input must survive reads and reconnects.
        assert flow.host.accept_input(flow.binding, 'unresolved-input')
        before = flow.view()
        (flow.run / 'arm-root-fault.json').write_text(json.dumps({'result': result}))
        first_identity = await flow.finish(first)
        await eventually(lambda: (flow.run / 'wire-fault-reply.json').exists(),
            'SETUP_FAULT_DID_NOT_REACH_QUEUED_DELIVERY')
        await eventually(lambda: any(alert['kind'] == 'compatibility'
            for alert in flow.view()['alerts']), 'MALFORMED_RESULT_NOT_REPORTED')
        count = polls(flow)
        await eventually(lambda: polls(flow) >= count + 3,
            'MALFORMED_RESULT_STOPPED_INVENTORY')
        held = flow.view()
        assert flow.supervisor.poll() is None
        assert held['service'] == 'live'
        assert held['main']['status'] == 'unknown'
        assert held['inputs'] == before['inputs']
        assert held['deliveries'][0]['attempts'] == []
        assert held['deliveries'][0]['completion_ids'] == [first_identity]
        assert flow.result_requests() == []
        operator = await flow.terminal('input', text='continue working',
            client_message_id='recovery-operator')
        assert 'result' in operator, operator
        await eventually(lambda: flow.view()['deliveries'][0]['status'] == 'confirmed',
            'FIRST_BATCH_DID_NOT_RECOVER')
        later_identity = await flow.finish(later)
        await eventually(lambda: len(flow.view()['deliveries']) == 2
            and all(batch['status'] == 'confirmed' for batch in flow.view()['deliveries']),
            'LATER_WORKER_DID_NOT_REACH_MAIN')
        after = flow.view()
        assert after['inputs']['unresolved-input'] == before['inputs']['unresolved-input']
        assert after['inputs']['recovery-operator']['status'] == 'accepted'
        assert after['deliveries'][0]['input'] == held['deliveries'][0]['input']
        assert after['deliveries'][0]['completion_ids'] == [first_identity]
        assert after['deliveries'][1]['completion_ids'] == [later_identity]
        assert all(len(batch['attempts']) == 1 for batch in after['deliveries'])
        requests = flow.result_requests()
        assert len(requests) == 2
        assert all(packet['method'] == 'turn/steer'
            and packet['params']['threadId'] == flow.root
            and packet['params']['expectedTurnId'] == after['main']['turn_id']
            for packet, _ in requests)
        native = [row['payload'] for row in flow.records() if row['kind'] == 'client-request']
        assert sum(packet['method'] == 'thread/start' for packet in native) == 1
        assert sum(packet['method'] == 'turn/start' for packet in native) == 2
        evidence = {'before': before, 'held': held, 'after': after,
            'polls_after_fault': count, 'polls_after_recovery': polls(flow),
            'native_results': requests, 'operator': operator}
        (ARTIFACTS / (name + '-evidence.json')).write_text(json.dumps(evidence, indent=2))
    finally:
        for name in ['wire-fault-selection.json', 'wire-fault-reply.json']:
            path = flow.run / name
            if path.exists():
                (ARTIFACTS / (flow.name + '-' + name)).write_bytes(path.read_bytes())
        await flow.close()


@pytest.mark.parametrize('mode', ['null', 'empty', 'drop'])
def test_postproposal_view_uncertainty_preserves_batch_and_recovers(mode):
    asyncio.run(view_recovery(mode))


async def view_recovery(mode):
    from t6_review_view import view_fault_proxy

    flow = ExternalFixture(name='review-view-' + mode)
    try:
        await flow.start()
        command = await flow.command()
        await flow.qualify([command])
        with view_fault_proxy(flow, mode) as (fired, wire):
            identity = await flow.finish(command)
            await eventually(fired.is_set, 'SETUP_POSTPROPOSAL_VIEW_FAULT_NOT_REACHED')
            await eventually(lambda: bool(flow.view()['alerts']), 'VIEW_PROBLEM_NOT_REPORTED')
            count = polls(flow)
            await eventually(lambda: polls(flow) >= count + 3, 'VIEW_FAULT_STOPPED_INVENTORY')
            held = flow.view()
            assert held['deliveries'][0]['attempts'] == []
            assert held['deliveries'][0]['completion_ids'] == [identity]
            assert flow.result_requests() == []
            assert flow.supervisor.poll() is None
            assert held['service'] == 'live'
            operator = await flow.terminal('input', text='continue after view recovery',
                client_message_id='view-recovery-operator')
            assert 'result' in operator, operator
            await eventually(lambda: flow.view()['deliveries'][0]['status'] == 'confirmed',
                'VIEW_UNCERTAINTY_DID_NOT_RECOVER')
            after = flow.view()
            assert after['deliveries'][0]['input'] == held['deliveries'][0]['input']
            assert after['deliveries'][0]['completion_ids'] == [identity]
            assert len(after['deliveries'][0]['attempts']) == 1
            assert len(flow.result_requests()) == 1
            (ARTIFACTS / (flow.name + '-evidence.json')).write_text(json.dumps({
                'wire': wire, 'held': held, 'after': after,
                'native_results': flow.result_requests()}, indent=2))
    finally:
        await flow.close()
