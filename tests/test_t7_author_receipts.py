"""Public listener invariants for retained native input and correlated acceptance."""
from dispatcher.runtime_control import RuntimeControl
from tests.t7_locked.receipt_events import history, normal_end
import pytest


def test_ordinary_steer_ack_cannot_change_the_durably_selected_turn(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review', conversation_id='root')['binding']
    assert control.event(binding, dict(type='service', status='live'))
    provenance = dict(method='turn/steer', thread_id='root', expected_turn_id='turn-a',
                      input=[dict(type='text', text='Keep the original whitespace.  ')])
    assert control.accept_input(binding, 'operator', native_input=provenance)
    before = control.view('owned', 17)
    assert not control.event(binding, dict(type='input/accepted', client_message_id='operator', turn_id='turn-b'))
    assert control.view('owned', 17) == before
    assert control.event(binding, dict(type='input/accepted', client_message_id='operator', turn_id='turn-a'))
    accepted = control.view('owned', 17)['inputs']['operator']
    assert accepted['status'] == 'accepted'
    assert accepted['native_input'] == provenance
    assert accepted['revision'] == before['inputs']['operator']['revision']


def test_history_settlement_does_not_revalidate_stop_that_precedes_new_input(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review', conversation_id='root')['binding']
    for event in [dict(type='service', status='live'),
                  dict(type='turn/started', thread_id='root', turn_id='old-stop'),
                  dict(type='turn/completed', thread_id='root', turn_id='old-stop', status='completed'),
                  dict(type='inventory', certainty='known', workers=[])]:
        assert control.event(binding, event)
    content = [dict(type='text', text='A new turn whose normal Stop was missed.')]
    assert control.accept_input(binding, 'operator', native_input=dict(
        method='turn/start', thread_id='root', expected_turn_id=None, input=content))
    assert control.event(binding, history('root', 'operator', 'new-turn', 'item', content))
    assert control.event(binding, normal_end('root', 'operator', 'new-turn', 'item', content))
    settled = control.view('owned', 17)
    assert settled['inputs']['operator']['status'] == 'settled'
    assert settled['main']['turn_id'] == 'old-stop'
    assert control.retire(binding, settled['revision']) == 'held'
    assert control.event(binding, dict(type='turn/started', thread_id='root', turn_id='current'))
    assert control.event(binding, dict(type='turn/completed', thread_id='root', turn_id='current', status='completed'))
    assert control.retire(binding, control.view('owned', 17)['revision']) == 'retired'


@pytest.mark.parametrize('kind', [[], {}])
def test_malformed_native_item_discriminant_rejects_without_partial_admission(tmp_path, kind):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review', conversation_id='root')['binding']
    before = control.view('owned', 17)
    assert not control.accept_input(binding, 'bad', native_input=dict(
        method='turn/start', thread_id='root', expected_turn_id=None,
        input=[dict(type=kind, text='Malformed item')]))
    assert control.view('owned', 17) == before


def test_old_rejection_replay_cannot_resolve_later_attempt_uncertainty():
    from tests.t7_locked.contract_fixture import ContractFixture
    fixture = ContractFixture()
    try:
        fixture.completion()
        fixture.active('a')
        fixture.propose()
        fixture.send(expected='a')
        rejection = dict(type='delivery/rejected', batch_id='batch-generic',
            attempt_id='attempt-generic', client_message_id='client-generic',
            rejection=dict(kind='expected-active-turn', code=-32600, message='no active turn to steer'))
        assert fixture.control.event(fixture.binding, rejection)
        fixture.active('b')
        fixture.send(attempt='attempt-b', client='client-b', expected='b')
        fixture.apply(dict(type='delivery/uncertain', batch_id='batch-generic',
            attempt_id='attempt-b', client_message_id='client-b', message='A new independent uncertain attempt.'))
        before = fixture.view()
        assert fixture.control.event(fixture.binding, rejection)
        assert fixture.view() == before
    finally:
        fixture.close()


def test_ordinary_operator_history_requires_exact_owning_turn_without_delivery(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owned', 17, 'review', conversation_id='root')['binding']
    content = [dict(type='text', text='An ordinary operator steer.  ')]
    assert control.accept_input(binding, 'operator', native_input=dict(
        method='turn/steer', thread_id='root', expected_turn_id='owning-turn', input=content))
    before = control.view('owned', 17)
    assert before['deliveries'] == []
    assert not control.event(binding, history('root', 'operator', 'wrong-turn', 'item', content))
    assert control.view('owned', 17) == before
    assert control.event(binding, history('root', 'operator', 'owning-turn', 'item', content))
    accepted = control.view('owned', 17)['inputs']['operator']
    assert accepted['status'] == 'accepted'
    assert accepted['turn_id'] == 'owning-turn'
    assert accepted['revision'] == before['inputs']['operator']['revision']
