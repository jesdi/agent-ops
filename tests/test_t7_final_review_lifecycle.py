"""Only an accepted initial lifecycle after admission can persist readiness proof."""
import pytest
from dispatcher.runtime_control import RuntimeControl


def prepare(control):
    binding = control.prepare('owned', 17, 'review')['binding']
    assert control.event(binding, dict(type='bootstrap/root-attempted', observed_revision=control.view('owned', 17)['revision'],
        root_operation_id='operation', root_method='thread/start', requested_conversation_id=None,
        client_message_id='initial', input=[dict(type='text', text='Initial prompt')]))
    assert control.event(binding, dict(type='bound', root_operation_id='operation', conversation_id='root'))
    return control.view('owned', 17)['binding']


def admit(control, binding):
    assert control.event(binding, dict(type='bootstrap/sent', root_operation_id='operation',
        client_message_id='initial', thread_id='root', observed_revision=control.view('owned', 17)['revision']))


@pytest.mark.parametrize('method', ['turn/started', 'turn/recovered'])
def test_preexisting_or_recovered_turn_does_not_prove_initial_readiness(tmp_path, method):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)
    if method == 'turn/recovered':
        admit(control, binding)
    assert control.event(binding, dict(type=method, thread_id='root', turn_id='old', status='inProgress'))
    if method == 'turn/started':
        admit(control, binding)
    before = control.view('owned', 17)
    assert 'initial_start' not in before['main']
    assert not control.event(binding, dict(type='turn/started', thread_id='root', turn_id='old'))
    assert not control.event(binding, dict(type='turn/started', thread_id='foreign', turn_id='new'))
    assert control.view('owned', 17) == before


def test_unrelated_admission_prevents_initial_lifecycle_readiness(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)
    admit(control, binding)
    assert control.accept_input(binding, 'operator')
    assert control.event(binding, dict(type='turn/started', thread_id='root', turn_id='operator-turn'))
    assert 'initial_start' not in control.view('owned', 17)['main']


def test_initial_start_proof_survives_exact_recovery_and_new_turn(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = prepare(control)
    admit(control, binding)
    assert control.event(binding, dict(type='turn/started', thread_id='root', turn_id='initial-turn'))
    proof = control.view('owned', 17)['main']['initial_start']
    assert control.event(binding, dict(type='turn/recovered', thread_id='root', turn_id='initial-turn', status='inProgress'))
    assert control.view('owned', 17)['main']['initial_start'] == proof
    assert control.event(binding, dict(type='turn/recovered', thread_id='root', turn_id='different-turn', status='inProgress'))
    assert control.view('owned', 17)['main']['initial_start'] == proof


@pytest.mark.parametrize('field,value', [
    ('client_message_id', 'foreign'), ('turn_id', 'foreign'),
    ('admission_revision', True), ('admission_revision', 0),
    ('revision', True), ('revision', 'unknown'), ('revision', 0), ('revision', 100000),
    ('extra', 'unrecognized'),
])
def test_corrupt_initial_start_provenance_is_managed_unknown(tmp_path, field, value):
    import json
    from tests.test_bound_turns_t2_acceptance import snapshot_file
    control = RuntimeControl(tmp_path)
    binding = prepare(control)
    admit(control, binding)
    assert control.event(binding, dict(type='turn/started', thread_id='root', turn_id='initial-turn'))
    path = snapshot_file(tmp_path, binding)
    snapshot = json.loads(path.read_text())
    snapshot['main']['initial_start'][field] = value
    path.write_text(json.dumps(snapshot))
    assert control.view('owned', 17)['main']['status'] == 'unknown'
    assert not control.event(binding, dict(type='service', status='live'))
