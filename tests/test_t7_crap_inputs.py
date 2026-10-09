"""Ordinary native input stays immutable through the public listener boundary."""
from copy import deepcopy

import pytest

from dispatcher.runtime_control import RuntimeControl


@pytest.mark.parametrize('item', [
    {'type': 'text', 'text': '  Exact text.  ', 'text_elements': []},
    {'type': 'text', 'text': 'tag', 'text_elements': [
        {'byteRange': {'start': 0, 'end': 3}, 'placeholder': 'tag'}]},
    {'type': 'image', 'url': 'https://example.invalid/image', 'detail': 'high'},
    {'type': 'image', 'fileId': 'file-generic'},
    {'type': 'localImage', 'path': '/generic/image.png', 'detail': 'original'},
    {'type': 'audio', 'url': 'https://example.invalid/audio'},
    {'type': 'localAudio', 'path': '/generic/audio.wav'},
    {'type': 'skill', 'name': 'generic', 'path': '/generic/SKILL.md'},
    {'type': 'mention', 'name': 'generic', 'path': '/generic/context'},
])
@pytest.mark.parametrize('method,expected', [('turn/start', None), ('turn/steer', 'active')])
def test_native_content_is_retained_and_acknowledged_without_normalization(tmp_path, item, method, expected):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    supplied = dict(method=method, thread_id='root', expected_turn_id=expected, input=[item])
    original = deepcopy(supplied)
    assert control.accept_input(binding, 'ordinary', native_input=supplied)
    supplied['input'].clear()
    admitted = control.view('fixture', 101)
    assert admitted['inputs']['ordinary']['native_input'] == original
    assert control.event(binding, dict(type='input/accepted', client_message_id='ordinary', turn_id='active'))
    receipt = control.view('fixture', 101)['inputs']['ordinary']
    assert receipt['native_input'] == original
    assert receipt['status'] == 'accepted'
    assert receipt['revision'] == admitted['inputs']['ordinary']['revision']


@pytest.mark.parametrize('item', [
    None, {'type': None}, {'type': 'unsupported'},
    {'type': 'text', 'text': 1}, {'type': 'text', 'text': '', 'text_elements': None},
    {'type': 'text', 'text': '', 'text_elements': [None]},
    {'type': 'image'}, {'type': 'image', 'url': 'generic', 'detail': 'unsupported'},
    {'type': 'localImage', 'path': 1}, {'type': 'audio', 'url': None},
    {'type': 'skill', 'name': 'generic'},
])
def test_malformed_native_item_does_not_consume_input_identity(tmp_path, item):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    before = control.view('fixture', 101)
    provenance = dict(method='turn/start', thread_id='root', expected_turn_id=None, input=[item])
    assert not control.accept_input(binding, 'ordinary', native_input=provenance)
    assert control.view('fixture', 101) == before
    provenance['input'] = []
    assert control.accept_input(binding, 'ordinary', native_input=provenance)
    assert control.view('fixture', 101)['inputs']['ordinary']['native_input']['input'] == []


@pytest.mark.parametrize('changes', [
    {'method': 'turn/start', 'expected_turn_id': 'unexpected'},
    {'method': 'turn/steer', 'expected_turn_id': None},
    {'method': 'turn/steer', 'expected_turn_id': ''},
    {'method': 'unsupported'}, {'thread_id': 'foreign'}, {'input': None},
])
def test_malformed_native_target_does_not_partially_admit(tmp_path, changes):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    before = control.view('fixture', 101)
    provenance = dict(method='turn/start', thread_id='root', expected_turn_id=None, input=[])
    provenance.update(changes)
    assert not control.accept_input(binding, 'ordinary', native_input=provenance)
    assert control.view('fixture', 101) == before


@pytest.mark.parametrize('event', [
    {'type': 'input/accepted', 'client_message_id': '', 'turn_id': 'active'},
    {'type': 'input/rejected', 'client_message_id': None},
    {'type': 'input/accepted', 'client_message_id': 'missing', 'turn_id': 'active'},
    {'type': 'input/accepted', 'client_message_id': 'ordinary', 'turn_id': None},
    {'type': 'input/accepted', 'client_message_id': 'ordinary', 'turn_id': ''},
])
def test_invalid_ack_preserves_pending_receipt_for_later_normal_ack(tmp_path, event):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    assert control.accept_input(binding, 'ordinary')
    before = control.view('fixture', 101)
    assert not control.event(binding, event)
    assert control.view('fixture', 101) == before
    valid = dict(type='input/accepted', client_message_id='ordinary', turn_id='active')
    assert control.event(binding, valid)
    accepted = control.view('fixture', 101)
    assert accepted['inputs']['ordinary']['status'] == 'accepted'
    assert not control.event(binding, valid)
    assert control.view('fixture', 101) == accepted


def test_dead_service_replay_is_stable_and_cannot_admit_or_revive(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    live = dict(type='service', status='live')
    assert control.event(binding, live)
    before_live_replay = control.view('fixture', 101)
    assert control.event(binding, live)
    assert control.view('fixture', 101)['revision'] == before_live_replay['revision'] + 1
    assert control.accept_input(binding, 'ordinary')
    dead = dict(type='service', status='dead')
    assert control.event(binding, dead)
    before = control.view('fixture', 101)
    assert control.event(binding, dead)
    assert not control.event(binding, live)
    assert not control.event(binding, dict(type='input/rejected', client_message_id='ordinary'))
    assert not control.accept_input(binding, 'later')
    assert control.view('fixture', 101) == before


def test_native_provenance_cannot_upgrade_existing_or_other_runtime_input(tmp_path):
    control = RuntimeControl(tmp_path)
    native = dict(method='turn/start', thread_id='root', expected_turn_id=None, input=[])
    binding = control.prepare('fixture', 101, 'review', conversation_id='root')['binding']
    assert control.accept_input(binding, 'legacy')
    before = control.view('fixture', 101)
    assert not control.accept_input(binding, 'legacy', native_input=native)
    assert control.view('fixture', 101) == before
    claude = control.prepare('fixture', 102, 'review', runtime='claude', conversation_id='root')['binding']
    before = control.view('fixture', 102)
    assert not control.accept_input(claude, 'ordinary', native_input=native)
    assert control.view('fixture', 102) == before
    assert control.accept_input(claude, 'ordinary')
