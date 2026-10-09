"""The stopped-work cap starts before a delayed native receipt arrives."""
from tests.t5_runtime_support import RuntimeCase, agent


def test_current_stop_starts_clock_while_ack_is_outstanding(tmp_path):
    case = RuntimeCase(tmp_path)
    assert case.control.accept_input(case.binding, 'initial')
    assert case.start('turn-B', now=90)
    assert case.stop('turn-B', now=100)
    assert case.inventory([agent(case)], now=100)
    assert case.view()['wait']['since'] == 100
    assert case.retire(now=11001) == 'held'
    assert case.send(dict(type='input/accepted', client_message_id='initial', turn_id='turn-B'), now=11000)
    assert case.inventory([agent(case)], now=11000)
    assert case.view()['wait']['since'] == 100
    assert case.retire(now=10900) == 'held'
    assert case.retire(now=11001) == 'retired'


def test_stop_before_new_input_cannot_start_clock(tmp_path):
    case = RuntimeCase(tmp_path)
    assert case.stop(now=90)
    assert case.control.accept_input(case.binding, 'new-input')
    assert case.inventory([agent(case)], now=100)
    assert case.view()['wait'] is None
    assert case.start('new-turn', now=200)
    assert case.stop('new-turn', now=210)
    assert case.inventory([agent(case)], now=210)
    assert case.view()['wait']['since'] == 210
    assert case.retire(now=20000) == 'held'
