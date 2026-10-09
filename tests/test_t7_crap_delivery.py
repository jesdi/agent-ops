"""Delivery reservations and recovery remain observable through RuntimeControl."""
import json

import pytest

from tests.t7_locked.contract_fixture import ContractFixture


@pytest.fixture
def contract():
    fixture = ContractFixture()
    try:
        fixture.completion()
        fixture.active('active')
        fixture.propose()
        yield fixture
    finally:
        fixture.close()


@pytest.mark.parametrize('selection', [None, {'source': 'thread/read', 'thread_id': 'foreign', 'status': 'idle'}])
def test_start_needs_exact_idle_selection_when_main_has_no_current_stop(contract, selection):
    event = dict(type='delivery/sent', observed_revision=contract.view()['revision'],
        batch_id='batch-generic', attempt_id='attempt', client_message_id='result',
        method='turn/start', thread_id=contract.root, expected_turn_id=None)
    if selection is not None:
        event['selection'] = selection
    before = contract.view()
    assert not contract.control.event(contract.binding, event)
    assert contract.view() == before
    event['selection'] = dict(source='thread/read', thread_id=contract.root, status='idle')
    assert contract.control.event(contract.binding, event)
    after = contract.view()
    assert after['main'] == before['main']
    assert after['inputs']['result']['status'] == 'pending'
    assert after['deliveries'][0]['attempts'][0]['expected_turn_id'] is None


def test_known_rejection_resolves_only_its_uncertainty_and_retry_remains_separate(contract):
    contract.send(expected='active')
    uncertain = dict(type='delivery/uncertain', batch_id='batch-generic',
        attempt_id='attempt-generic', client_message_id='client-generic', message='Acceptance unknown.')
    contract.apply(uncertain)
    rejection = dict(type='delivery/rejected', batch_id='batch-generic',
        attempt_id='attempt-generic', client_message_id='client-generic',
        rejection=dict(kind='expected-active-turn', code=-32600, message='no active turn to steer'))
    before = contract.view()
    for changes in ({'batch_id': 'missing'}, {'attempt_id': 'missing'}, {'client_message_id': 'foreign'}):
        assert not contract.control.event(contract.binding, dict(rejection, **changes))
        assert contract.view() == before
    contract.apply(rejection)
    rejected = contract.view()
    assert rejected['alerts'][0]['status'] == 'resolved'
    assert rejected['deliveries'][0]['status'] == 'pending'
    assert rejected['inputs']['client-generic']['status'] == 'rejected'
    contract.send(attempt='second', client='second', expected='active')
    contract.apply(dict(uncertain, attempt_id='second', client_message_id='second'))
    before = contract.view()
    assert len(before['alerts']) == 1
    assert before['alerts'][0]['status'] == 'pending'
    contract.apply(rejection)
    assert contract.view() == before
    contract.ack(attempt='second', client='second', turn='active')
    confirmed = contract.view()
    assert confirmed['alerts'][0]['status'] == 'resolved'
    assert confirmed['deliveries'][0]['status'] == 'confirmed'


@pytest.mark.parametrize('changes', [
    {'receipt': None},
    {'rejection': {'kind': 'expected-active-turn', 'code': -32600, 'message': 'no active turn to steer'}},
])
def test_corrupt_confirmation_is_unknown_and_cannot_mutate_until_exact_storage_restored(contract, changes):
    contract.send(expected='active')
    contract.ack(turn='active')
    before = contract.view()
    path, = contract.state.rglob(contract.binding['launch_id'] + '.json')
    original = path.read_bytes()
    damaged = json.loads(original)
    damaged['deliveries'][0]['attempts'][0].update(changes)
    path.write_text(json.dumps(damaged))
    corrupt = path.read_bytes()
    try:
        unknown = contract.view()
        assert unknown['main']['status'] == 'unknown'
        assert unknown['inventory'] == 'unknown'
        assert 'inputs' not in unknown
        assert not contract.control.event(contract.binding, dict(type='service', status='live'))
        assert path.read_bytes() == corrupt
    finally:
        path.write_bytes(original)
    assert contract.view() == before
