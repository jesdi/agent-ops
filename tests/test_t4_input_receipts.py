"""Input receipts through the approved RuntimeControl event/admission seam."""
from dispatcher.runtime_control import RuntimeControl


def test_one_turn_stop_cannot_release_another_unconfirmed_input(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.accept_input(binding, 'first')
    assert control.accept_input(binding, 'second')
    assert control.event(binding, {'type': 'input/accepted', 'client_message_id': 'first', 'turn_id': 'turn-A'})
    assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'turn-A'})
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root', 'turn_id': 'turn-A', 'status': 'completed'})
    restored = RuntimeControl(tmp_path)
    snapshot = restored.view('project', 1)
    assert restored.retire(binding, snapshot['revision']) == 'held'
    assert restored.event(binding, {'type': 'input/rejected', 'client_message_id': 'second'})
    snapshot = restored.view('project', 1)
    assert restored.retire(binding, snapshot['revision']) == 'retired'


def test_launch_tagged_foreign_callback_cannot_create_legacy_wait(tmp_path):
    import json
    from dispatcher.waitd import handle_ping
    from dispatcher.state import has_waiting, read_session
    control = RuntimeControl(tmp_path)
    binding = control.prepare('owner', 1, 'review', runtime='claude', conversation_id='root')['binding']
    payload = dict(target='legacy', issue=2, launch_id=binding['launch_id'],
                   hook_event_name='UserPromptSubmit', session_id='root', prompt_id='prompt',
                   stage='review', ticket='')
    assert handle_ping(json.dumps(payload).encode(), tmp_path) is False
    assert not has_waiting(tmp_path, 'legacy', 2)
    assert read_session(tmp_path, 'legacy', 2) is None


def test_missing_receipt_inventory_is_unknown_after_storage_loss(tmp_path):
    import json
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.accept_input(binding, 'pending')
    path, = tmp_path.glob('runtime/*/' + binding['launch_id'] + '.json')
    snapshot = json.loads(path.read_text())
    del snapshot['inputs']
    path.write_text(json.dumps(snapshot))
    restored = control.view('project', 1)
    assert restored['binding'] == {}
    assert control.retire(binding, snapshot['revision'], reason='forced') == 'unknown'


def test_delayed_ack_for_completed_old_turn_settles_only_its_input(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.accept_input(binding, 'first')
    assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'turn-A'})
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root', 'turn_id': 'turn-A', 'status': 'completed'})
    assert control.accept_input(binding, 'second')
    assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'turn-B'})
    control = RuntimeControl(tmp_path)
    assert control.event(binding, {'type': 'input/accepted', 'client_message_id': 'first', 'turn_id': 'turn-A'})
    assert control.retire(binding, control.view('project', 1)['revision']) == 'held'
    assert control.event(binding, {'type': 'input/accepted', 'client_message_id': 'second', 'turn_id': 'turn-B'})
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root', 'turn_id': 'turn-B', 'status': 'completed'})
    assert control.retire(binding, control.view('project', 1)['revision']) == 'retired'


def test_ack_for_new_input_cannot_reuse_a_stop_before_its_admission(tmp_path):
    control = RuntimeControl(tmp_path)
    binding = control.prepare('project', 1, 'review', conversation_id='root')['binding']
    assert control.event(binding, {'type': 'service', 'status': 'live'})
    assert control.event(binding, {'type': 'inventory', 'certainty': 'known', 'workers': []})
    assert control.event(binding, {'type': 'turn/started', 'thread_id': 'root', 'turn_id': 'turn-A'})
    assert control.event(binding, {'type': 'turn/completed', 'thread_id': 'root', 'turn_id': 'turn-A', 'status': 'completed'})
    assert control.accept_input(binding, 'new-input')
    assert control.event(binding, {'type': 'input/accepted', 'client_message_id': 'new-input', 'turn_id': 'turn-A'})
    assert control.retire(binding, control.view('project', 1)['revision']) == 'held'
