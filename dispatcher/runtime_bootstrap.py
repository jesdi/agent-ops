"""Durable permission for the single initial root and prompt of a launch."""
from copy import deepcopy

from dispatcher.runtime_work import nonempty


def initial_text(value):
    return (isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and set(item) == {'type', 'text'}
        and item['type'] == 'text' and isinstance(item['text'], str) for item in value))


def root_target(method, requested):
    return ((method == 'thread/start' and requested is None)
            or (method == 'thread/resume' and nonempty(requested)))


def valid_bootstrap(snapshot):
    record = snapshot.get('bootstrap')
    if record is None:
        return True
    if not isinstance(record, dict) or snapshot['binding']['runtime'] != 'codex':
        return False
    initial = record.get('initial_input')
    if not isinstance(initial, dict):
        return False
    revision = record.get('root_attempt_revision')
    valid = all((nonempty(record.get('root_operation_id')),
        root_target(record.get('root_method'), record.get('requested_conversation_id')),
        type(revision) is int and 0 < revision <= snapshot['revision'],
        nonempty(initial.get('client_message_id')), initial_text(initial.get('input')),
        record.get('root_status') in ('attempted-unconfirmed', 'bound')))
    return valid and valid_root_binding(record, snapshot['binding']['conversation_id'])


def valid_root_binding(record, root):
    if record['root_method'] == 'thread/resume':
        return record['requested_conversation_id'] == root
    return (root is None) == (record['root_status'] == 'attempted-unconfirmed')


def attempt_root(snapshot, event):
    candidate = dict(root_operation_id=event.get('root_operation_id'),
        root_method=event.get('root_method'),
        requested_conversation_id=event.get('requested_conversation_id'),
        initial_input=dict(client_message_id=event.get('client_message_id'), input=event.get('input')))
    previous = snapshot.get('bootstrap')
    if isinstance(previous, dict):
        return all(previous[k] == v for k, v in candidate.items())
    if 'bootstrap' not in snapshot or not fresh_revision(snapshot, event):
        return False
    root = snapshot['binding']['conversation_id']
    if candidate['requested_conversation_id'] != root:
        return False
    candidate.update(root_attempt_revision=snapshot['revision'] + 1, root_status='attempted-unconfirmed')
    check = dict(snapshot, bootstrap=candidate, revision=snapshot['revision'] + 1)
    if not valid_bootstrap(check):
        return False
    snapshot['bootstrap'] = deepcopy(candidate)
    return True


def fresh_revision(snapshot, event):
    revision = event.get('observed_revision')
    return type(revision) is int and revision == snapshot['revision']


def bind_root(snapshot, event):
    record = snapshot.get('bootstrap')
    root = event.get('conversation_id')
    if not nonempty(root):
        return False
    if record is None:
        if 'root_operation_id' in event or snapshot['binding']['conversation_id'] is not None:
            return False
    else:
        if event.get('root_operation_id') != record['root_operation_id']:
            return False
        expected = record['requested_conversation_id'] or snapshot['binding']['conversation_id']
        if expected is not None and root != expected:
            return False
        record['root_status'] = 'bound'
    snapshot['binding']['conversation_id'] = root
    return True


def send_initial(snapshot, event):
    record = snapshot.get('bootstrap')
    if not isinstance(record, dict) or record['root_status'] != 'bound':
        return False
    identity = record['initial_input']['client_message_id']
    if not all((event.get('root_operation_id') == record['root_operation_id'],
                event.get('client_message_id') == identity,
                event.get('thread_id') == snapshot['binding']['conversation_id'])):
        return False
    if identity in snapshot['inputs']:
        return True
    if not fresh_revision(snapshot, event):
        return False
    snapshot['inputs'][identity] = dict(status='pending', turn_id=None, revision=snapshot['revision'] + 1)
    return True


def pending_initial(snapshot):
    record = snapshot.get('bootstrap')
    if not isinstance(record, dict) or record['root_status'] != 'bound':
        return None
    identity = record['initial_input']['client_message_id']
    initial = snapshot['inputs'].get(identity)
    if (initial is None or initial['status'] != 'pending'
            or any(receipt['revision'] > initial['revision']
                   for receipt in snapshot['inputs'].values())):
        return None
    return dict(client_message_id=identity, admission_revision=initial['revision'])


def record_initial_start(snapshot, turn):
    if 'initial_start' in snapshot['main']:
        return
    proof = pending_initial(snapshot)
    if proof is not None:
        snapshot['main']['initial_start'] = dict(proof, turn_id=turn,
                                                revision=snapshot['revision'] + 1)


def valid_initial_start(snapshot):
    main = snapshot['main']
    if 'initial_start' not in main:
        return True
    proof = main['initial_start']
    record = snapshot.get('bootstrap')
    if not valid_start_proof(proof) or not isinstance(record, dict):
        return False
    identity = record['initial_input']['client_message_id']
    initial = snapshot['inputs'].get(identity)
    if initial is None or not main['seen_turns']:
        return False
    return all((record['root_status'] == 'bound', proof['client_message_id'] == identity,
        proof['turn_id'] in main['seen_turns'],
        proof['admission_revision'] == initial['revision'],
        initial['revision'] < proof['revision'] <= snapshot['revision']))


def valid_start_proof(proof):
    return (isinstance(proof, dict)
            and set(proof) == {'client_message_id', 'admission_revision', 'turn_id', 'revision'}
            and type(proof['admission_revision']) is int and type(proof['revision']) is int)


def initial_lifecycle_ready(snapshot):
    proof = snapshot['main'].get('initial_start')
    return proof is not None
