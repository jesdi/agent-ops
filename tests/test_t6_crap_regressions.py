"""T6 behavior through public runtime and running supervisor boundaries."""
from copy import deepcopy
import json

import pytest

from dispatcher.state import Stage, TaskState, save
from t5_observed_session import ObservedSession
from t6_locked.contract_fixture import ContractFixture


@pytest.fixture
def contract():
    fixture = ContractFixture()
    try:
        fixture.completion()
        yield fixture
    finally:
        fixture.close()


def test_delivery_replay_is_revision_stable_and_native_events_still_advance(contract):
    proposal = contract.propose()
    before = contract.view()
    assert contract.control.event(contract.binding, proposal)
    assert contract.view() == before
    assert contract.control.event(contract.binding, {'type': 'service', 'status': 'live'})
    after = contract.view()
    assert after['revision'] == before['revision'] + 1
    assert after['deliveries'] == before['deliveries']
    assert not contract.control.event(contract.binding, {'type': 'delivery/unsupported'})
    assert contract.view() == after


def publish_task(session):
    save(session.state, TaskState(issue=501, target='fixture', stage=Stage.REVIEW,
        slot=0, worktree=str(session.worktree), branch='fixture', title='Generic task',
        updated_at='2026-10-08T00:00:00Z'))
    (session.worktree / '.agent').mkdir(exist_ok=True)
    (session.worktree / '.agent/stage.json').write_text(json.dumps({'stage': 'review', 'status': 'working'}))


def qualify_command(session, item='generic-item'):
    session.backend('start_command', thread_id=session.root, turn_id=session.initial_turn, item_id=item)
    session.backend('end_turn', thread_id=session.root, turn_id=session.initial_turn)
    assert session.until(lambda: any(w['identity'].get('initial_item_id') == item
        and w['normal_stop'] is not None for w in session.view()['workers']))


def finish_command(session, item='generic-item'):
    session.backend('end_command', thread_id=session.root, item_id=item,
        status='failed', exit_code=7, output='generic failure', duration_ms=0)


def result_requests(session):
    return [row for row in session.rows() if row['kind'] == 'rpc_received'
        and row['method'] in ('turn/start', 'turn/steer')
        and any('generic failure' in part.get('text', '') for part in row['params'].get('input', []))]


@pytest.mark.parametrize('active', [False, True], ids=['idle-start', 'active-steer'])
def test_result_is_reserved_before_exact_root_forward_and_confirmed(monkeypatch, active):
    with ObservedSession(monkeypatch) as session:
        publish_task(session)
        qualify_command(session)
        expected = None
        if active:
            response = session.terminal('rpc', method='turn/start', params={
                'threadId': session.root, 'clientUserMessageId': 'operator-generic',
                'input': [{'type': 'text', 'text': 'operator input'}]})
            expected = response['result']['turn']['id']
        method = 'turn/steer' if active else 'turn/start'
        session.backend('hold_next', name='inspect-reservation', phase='before',
            matches={'method': method, 'threadId': session.root})
        finish_command(session)
        session.backend('wait', matches={'kind': 'request_held', 'name': 'inspect-reservation'})
        before = session.view()
        batch = before['deliveries'][0]
        attempt = batch['attempts'][0]
        assert attempt['status'] == 'sent-unconfirmed'
        assert attempt['thread_id'] == session.root
        assert attempt['expected_turn_id'] == expected
        assert before['inputs'][attempt['client_message_id']] == {
            'status': 'pending', 'turn_id': None, 'revision': attempt['admission_revision']}
        request = result_requests(session)[0]
        assert request['method'] == method
        assert request['params']['threadId'] == session.root
        assert request['params'].get('expectedTurnId') == expected
        assert request['params']['input'] == batch['input']
        assert json.loads(batch['input'][0]['text']) == [{
            'identity': {'kind': 'command', 'thread_id': session.root, 'initial_item_id': 'generic-item'},
            'outcome': {'status': 'failed', 'exit_code': 7, 'aggregated_output': 'generic failure', 'duration_ms': 0}}]
        session.backend('release', name='inspect-reservation')
        assert session.until(lambda: session.view()['deliveries'][0]['status'] == 'confirmed')
        after = session.view()
        assert after['deliveries'][0]['input'] == batch['input']
        assert after['inputs'][attempt['client_message_id']]['status'] == 'accepted'
        assert len(result_requests(session)) == 1


@pytest.mark.parametrize('bad_read', [
    'missing-thread', 'foreign-root', 'invalid-turn', 'system-error',
    'idle-active-turn', 'active-empty', 'active-multiple', 'active-not-latest',
])
def test_uncertain_root_read_keeps_batch_unsent_until_fresh_authority(monkeypatch, bad_read):
    with ObservedSession(monkeypatch) as session:
        qualify_command(session)
        finish_command(session)
        assert session.until(lambda: len(session.view()['completions']) == 1)
        records = deepcopy(session.view()['completions'])
        thread = deepcopy(session.backend('snapshot')['threads'][session.root])
        if bad_read == 'missing-thread':
            thread = None
        elif bad_read == 'foreign-root':
            thread['id'] = 'foreign-root'
        elif bad_read == 'invalid-turn':
            thread['turns'][0]['status'] = 'unsupported'
        elif bad_read == 'system-error':
            thread['status'] = {'type': 'systemError'}
        elif bad_read == 'idle-active-turn':
            thread['turns'][0]['status'] = 'inProgress'
        else:
            thread['status'] = {'type': 'active', 'activeFlags': []}
            if bad_read == 'active-empty':
                thread['turns'] = []
            elif bad_read == 'active-multiple':
                thread['turns'] = [{'id': 'active-a', 'status': 'inProgress', 'items': []},
                                   {'id': 'active-b', 'status': 'inProgress', 'items': []}]
            else:
                thread['turns'] = [{'id': 'active-a', 'status': 'inProgress', 'items': []},
                                   {'id': 'later-completed', 'status': 'completed', 'items': []}]
        session.backend('fault_next', matches={'method': 'thread/read', 'threadId': session.root},
            fault={'result': {'thread': thread}}, times=1000)
        publish_task(session)
        assert session.until(lambda: len(session.view()['deliveries']) == 1)
        reads = sum(r['kind'] == 'rpc_received' and r['method'] == 'thread/read' for r in session.rows())
        assert session.until(lambda: sum(r['kind'] == 'rpc_received' and r['method'] == 'thread/read'
            for r in session.rows()) >= reads + 4)
        held = session.view()
        assert held['deliveries'][0]['attempts'] == []
        assert held['completions'] == records
        assert result_requests(session) == []
        session.backend('clear_faults')
        assert session.until(lambda: session.view()['deliveries'][0]['status'] == 'confirmed')
        assert len(result_requests(session)) == 1
        assert session.view()['deliveries'][0]['input'] == held['deliveries'][0]['input']


@pytest.mark.parametrize('active,fault', [
    (False, {'result': None}),
    (False, {'result': {'turn': {'id': 'wrong', 'status': 'unsupported', 'items': []}}}),
    (True, {'result': {'turnId': 'wrong-turn'}}),
    (True, {'error': {'code': -32603, 'message': 'failure after acceptance'}}),
])
def test_uncertain_ack_preserves_pending_batch_while_operator_and_new_batch_progress(monkeypatch, active, fault):
    with ObservedSession(monkeypatch) as session:
        publish_task(session)
        session.backend('start_command', thread_id=session.root, turn_id=session.initial_turn, item_id='second-item')
        qualify_command(session)
        if active:
            session.terminal('rpc', method='turn/start', params={'threadId': session.root,
                'clientUserMessageId': 'operator-start', 'input': [{'type': 'text', 'text': 'operator start'}]})
        method = 'turn/steer' if active else 'turn/start'
        session.backend('fault_next', matches={'method': method, 'threadId': session.root}, fault=fault)
        # Keep acceptance genuinely unknown while testing independent progress.
        # T7 may otherwise recover this exact receipt from readable active history.
        session.backend('fault_next', matches={'method': 'thread/items/list', 'threadId': session.root},
            fault={'result': {'data': [], 'nextCursor': None}}, times=1000)
        finish_command(session)
        assert session.until(lambda: len(result_requests(session)) == 1)
        assert session.until(lambda: any(r['kind'] == 'rpc_response' and r['method'] == method
            and all(r['packet'].get(k) == v for k, v in fault.items()) for r in session.rows()))
        before = session.view()
        first = deepcopy(before['deliveries'][0])
        attempt = first['attempts'][0]
        assert attempt['status'] == 'sent-unconfirmed'
        assert before['inputs'][attempt['client_message_id']]['status'] == 'pending'
        native = session.backend('snapshot')['threads'][session.root]
        current_turn = native['turns'][-1]['id']
        response = session.terminal('rpc', method='turn/steer', params={'threadId': session.root,
            'expectedTurnId': current_turn, 'clientUserMessageId': 'operator-progress',
            'input': [{'type': 'text', 'text': 'operator progress'}]})
        assert response['result']['turnId'] == current_turn
        finish_command(session, 'second-item')
        assert session.until(lambda: len(session.view()['deliveries']) == 2
            and session.view()['deliveries'][1]['status'] == 'confirmed')
        after = session.view()
        assert after['deliveries'][0] == first
        assert after['inputs'][attempt['client_message_id']]['status'] == 'pending'
        assert after['inputs']['operator-progress']['status'] == 'accepted'
        requests = result_requests(session)
        assert len(requests) == 2
        assert requests[1]['method'] == 'turn/steer'
        assert requests[1]['params']['expectedTurnId'] == current_turn
        assert requests[1]['params']['threadId'] == session.root
        session.backend('clear_faults')
        assert session.until(lambda: session.view()['deliveries'][0]['status'] == 'confirmed')
        recovered = session.view()['deliveries'][0]
        assert recovered['attempts'][0]['receipt']['source'] == 'history'
        assert recovered['attempts'][0]['attempt_id'] == attempt['attempt_id']
        assert recovered['attempts'][0]['admission_revision'] == attempt['admission_revision']
        assert recovered['input'] == first['input']
        assert len(result_requests(session)) == 2


@pytest.mark.parametrize('content', [None, [], [{'type': 'image', 'text': 'not a result'}]])
def test_invalid_result_input_is_rejected_without_consuming_completion(contract, content):
    event = contract.proposal()
    event['input'] = content
    before = contract.view()
    assert not contract.control.event(contract.binding, event)
    assert contract.view() == before
    valid = contract.propose()
    assert contract.batch()['input'] == valid['input']
    assert contract.view()['completions'] == before['completions']


@pytest.mark.parametrize('corruption', [
    'attempts-not-list', 'unsupported-batch-status', 'non-object-attempt',
    'empty-attempt-id', 'future-admission', 'confirmed-with-pending-attempt',
])
def test_corrupt_delivery_storage_reads_unknown_until_exact_bytes_restored(contract, corruption):
    contract.active()
    contract.propose()
    contract.send()
    before = contract.view()
    assert before['inventory'] == 'known'
    assert before['deliveries'][0]['attempts'][0]['status'] == 'sent-unconfirmed'
    assert before['inputs']['client-generic']['status'] == 'pending'
    # This is external corruption of this test's own host state, never a normal writer.
    path, = contract.state.rglob(contract.binding['launch_id'] + '.json')
    original = path.read_bytes()
    damaged = json.loads(original)
    batch = damaged['deliveries'][0]
    if corruption == 'attempts-not-list':
        batch['attempts'] = None
    elif corruption == 'unsupported-batch-status':
        batch['status'] = 'unsupported'
    elif corruption == 'non-object-attempt':
        batch['attempts'][0] = None
    elif corruption == 'empty-attempt-id':
        batch['attempts'][0]['attempt_id'] = ''
    elif corruption == 'future-admission':
        batch['attempts'][0]['admission_revision'] = before['revision'] + 1
    else:
        batch['status'] = 'confirmed'
    path.write_text(json.dumps(damaged))
    corrupt_bytes = path.read_bytes()
    try:
        unknown = contract.view()
        assert unknown['main']['status'] == 'unknown'
        assert unknown['inventory'] == 'unknown'
        assert unknown['wait'] is None
        assert 'inputs' not in unknown
        assert not contract.control.event(contract.binding, {'type': 'service', 'status': 'live'})
        assert contract.control.retire(contract.binding, before['revision']) == 'unknown'
        assert path.read_bytes() == corrupt_bytes
    finally:
        path.write_bytes(original)
    assert contract.view() == before


def test_committed_listener_response_loss_prevents_native_forward(monkeypatch):
    from test_t6_author_delivery import lose_sent_response

    with ObservedSession(monkeypatch) as session:
        publish_task(session)
        qualify_command(session)
        with lose_sent_response(session.state) as lost:
            finish_command(session)
            assert session.until(lost.is_set)
            before = session.view()
            batch = before['deliveries'][0]
            attempt = batch['attempts'][0]
            assert attempt['status'] == 'sent-unconfirmed'
            assert before['inputs'][attempt['client_message_id']]['status'] == 'pending'
            scans = sum(r['kind'] == 'rpc_received' and r['method'] == 'thread/list' for r in session.rows())
            assert session.until(lambda: sum(r['kind'] == 'rpc_received' and r['method'] == 'thread/list'
                for r in session.rows()) > scans + 2)
            assert session.view()['deliveries'] == before['deliveries']
            assert session.view()['completions'] == before['completions']
            assert result_requests(session) == []
