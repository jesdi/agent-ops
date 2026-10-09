"""Stored lifecycle evidence must remain authoritative after listener restart."""
import json

import pytest

from dispatcher.runtime_control import RuntimeControl


def stopped_launch(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'A'})
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root',
                                   'turn_id': 'A', 'status': 'completed'})
    path, = tmp_path.glob('runtime/*/' + binding['launch_id'] + '.json')
    return binding, path


def test_future_end_cannot_release_new_input_after_restart(tmp_path):
    binding, path = stopped_launch(tmp_path)
    damaged = json.loads(path.read_text())
    damaged['main']['completed_turns']['A'] = damaged['revision'] + 100
    path.write_text(json.dumps(damaged))
    control = RuntimeControl(tmp_path)
    assert control.view('project', 1)['binding'] == {}
    assert not control.accept_input(binding, 'new-input')
    assert not control.event(binding, {'type': 'input/accepted',
                                       'client_message_id': 'new-input', 'turn_id': 'A'})
    assert control.retire(binding, damaged['revision']) == 'unknown'
    assert json.loads(path.read_text()) == damaged


@pytest.mark.parametrize('main_changes,inputs', [
    ({'completed_turns': {}}, {}),
    ({'completed_turns': {'A': 4, 'unseen': 3}}, {}),
    ({'completed_turns': {'A': 0}}, {}),
    ({'seen_turns': ['A', 'B']}, {}),
    ({'seen_turns': ['A', 'A']}, {}),
    ({'status': 'active'}, {}),
    ({}, {'message': {'status': 'accepted', 'turn_id': None, 'revision': 2}}),
    ({}, {'message': {'status': 'settled', 'turn_id': None, 'revision': 2}}),
    ({}, {'message': {'status': 'pending', 'turn_id': 'A', 'revision': 2}}),
    ({}, {'message': {'status': 'rejected', 'turn_id': 'A', 'revision': 2}}),
    ({}, {'message': {'status': 'pending', 'turn_id': None, 'revision': 5}}),
    ({}, {'message': {'status': 'pending', 'turn_id': None, 'revision': 0}}),
    ({}, {'message': {'status': 'settled', 'turn_id': 'unseen', 'revision': 2}}),
    ({'seen_turns': ['B', 'A']},
     {'message': {'status': 'settled', 'turn_id': 'B', 'revision': 2}}),
    ({}, {'message': {'status': 'settled', 'turn_id': 'A', 'revision': 4}}),
    ({'completed_turns': {'A': 2}},
     {'message': {'status': 'settled', 'turn_id': 'A', 'revision': 3}}),
], ids=[
    'stopped-without-normal-end', 'unseen-normal-end', 'end-before-first-event',
    'stopped-current-is-not-latest', 'duplicate-turn-history', 'active-already-ended',
    'accepted-without-turn', 'settled-without-turn', 'pending-with-turn',
    'rejected-with-turn', 'future-admission', 'admission-before-first-event',
    'settled-unseen-turn', 'settled-uncompleted-turn', 'settled-same-revision',
    'settled-before-admission',
])
def test_inconsistent_lifecycle_storage_has_no_authority(tmp_path, main_changes, inputs):
    binding, path = stopped_launch(tmp_path)
    damaged = json.loads(path.read_text())
    damaged['main'].update(main_changes)
    damaged['inputs'] = inputs
    path.write_text(json.dumps(damaged))
    control = RuntimeControl(tmp_path)
    assert control.view('project', 1)['binding'] == {}
    assert control.retire(binding, damaged['revision']) == 'unknown'
    assert control.retire(binding, damaged['revision'], reason='forced') == 'unknown'
    assert not control.accept_input(binding, 'new')
    assert not control.event(binding, {'type': 'service', 'status': 'live'})
    assert json.loads(path.read_text()) == damaged


def test_recovery_cannot_revive_observed_normal_end_or_replace_snapshot(tmp_path):
    binding, path = stopped_launch(tmp_path)
    control = RuntimeControl(tmp_path)
    assert control.event(binding, {'type': 'control/unknown', 'message': 'connection lost'})
    before = path.read_bytes()
    inode = path.stat().st_ino
    assert not control.event(binding, {'type': 'turn/recovered', 'thread_id': 'root',
                                       'turn_id': 'A', 'status': 'inProgress'})
    assert path.read_bytes() == before
    assert path.stat().st_ino == inode
    snapshot = RuntimeControl(tmp_path).view('project', 1)
    assert snapshot['binding'] == binding
    assert snapshot['main']['status'] == 'unknown'
    assert snapshot['main']['completed_turns'] == {'A': 4}
    assert control.retire(binding, snapshot['revision']) == 'held'


@pytest.mark.parametrize('already_started', [False, True], ids=['unseen-ack', 'active-root'])
def test_receipt_survives_unknown_and_active_root_recovery(tmp_path, already_started):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.accept_input(binding, 'first-input')
    assert control.view('project', 1)['inputs']['first-input']['revision'] == 1
    assert control.event(binding, {'type': 'input/accepted',
                                   'client_message_id': 'first-input', 'turn_id': 'A'})
    if already_started:
        assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'A'})
    assert control.event(binding, {'type': 'control/unknown', 'message': 'connection lost'})
    control = RuntimeControl(tmp_path)
    assert control.event(binding, {'type': 'turn/recovered', 'thread_id': 'root',
                                   'turn_id': 'A', 'status': 'inProgress'})
    recovered = control.view('project', 1)
    assert recovered['main']['status'] == 'active'
    assert recovered['main']['completed_turns'] == {}
    assert recovered['inputs']['first-input']['status'] == 'accepted'
    assert control.retire(binding, recovered['revision']) == 'held'
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root',
                                   'turn_id': 'A', 'status': 'completed'})
    ended = RuntimeControl(tmp_path).view('project', 1)
    assert ended['main']['completed_turns']['A'] == ended['revision']
    assert ended['inputs']['first-input']['status'] == 'settled'
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.retire(binding, control.view('project', 1)['revision']) == 'retired'


@pytest.mark.parametrize('main_changes,inputs', [
    ({}, {}),
    ({'status': 'active', 'completed_turns': {}}, {}),
    ({'status': 'unknown', 'turn_id': None}, {}),
    ({'status': 'unknown', 'turn_id': None, 'seen_turns': [], 'completed_turns': {}},
     {'message': {'status': 'accepted', 'turn_id': 'A', 'revision': 1}}),
    ({}, {'message': {'status': 'settled', 'turn_id': 'A', 'revision': 1}}),
], ids=['stopped', 'active', 'unknown-history', 'accepted-unseen', 'settled'])
def test_turn_evidence_without_bound_conversation_is_unknown(tmp_path, main_changes, inputs):
    binding, path = stopped_launch(tmp_path)
    damaged = json.loads(path.read_text())
    damaged['binding']['conversation_id'] = None
    damaged['main'].update(main_changes)
    damaged['inputs'] = inputs
    path.write_text(json.dumps(damaged))
    control = RuntimeControl(tmp_path)
    assert control.view('project', 1)['binding'] == {}
    assert not control.accept_input(damaged['binding'], 'new-input')
    assert control.retire(damaged['binding'], damaged['revision']) == 'unknown'
    assert json.loads(path.read_text()) == damaged


def test_unbound_launch_can_reserve_input_and_record_health_before_binding(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review')['binding']
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.accept_input(binding, 'first-input')
    snapshot = RuntimeControl(tmp_path).view('project', 1)
    assert snapshot['binding'] == binding
    assert snapshot['inputs']['first-input']['status'] == 'pending'
    assert control.retire(binding, snapshot['revision']) == 'held'
    assert control.event(binding, {'type': 'bound', 'conversation_id': 'root'})
    bound = control.view('project', 1)['binding']
    assert control.event(bound, {'type': 'input/accepted',
                                 'client_message_id': 'first-input', 'turn_id': 'A'})
