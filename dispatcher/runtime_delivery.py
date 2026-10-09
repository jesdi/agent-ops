"""Launch-bound immutable result assignments and atomic input reservations.

Only RuntimeControl applies these transitions, under the listener's writer lock.
"""
from copy import deepcopy
import json
import re

from dispatcher.runtime_work import identity_key, nonempty, valid_identity, inputs_resolved
from dispatcher.state import load, read_stage_signal, RESPAWNABLE_STAGES, Stage


def unassigned(snapshot):
    assigned = {identity_key(i) for b in snapshot['deliveries'] for i in b['completion_ids']}
    return [r for r in snapshot['completions'] if identity_key(r['identity']) not in assigned]


def results_resolved(snapshot):
    return not unassigned(snapshot) and all(b['status'] == 'confirmed' for b in snapshot['deliveries'])


def task_matches(task, binding):
    if task is None:
        return False
    return all((task.target == binding['target'], task.issue == binding['issue'],
                task.worktree == binding['worktree'], task.stage in RESPAWNABLE_STAGES,
                task.continued_stage.value == binding['stage'], not task.park,
                task.stage != Stage.IMPLEMENT or str(task.ticket_cursor) == binding['ticket']))


def gate_open(snapshot, state_dir):
    binding = snapshot['binding']
    if snapshot['service'] != 'live' or snapshot['retired']:
        return False
    try:
        task = load(state_dir, binding['target'], binding['issue'])
        signal = read_stage_signal(binding['worktree'])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (task_matches(task, binding) and signal is not None
            and signal.stage == binding['stage'] and signal.status == 'working')


def valid_members(identities):
    if not isinstance(identities, list) or not identities:
        return False
    return (all(valid_identity(i) for i in identities)
            and len({identity_key(i) for i in identities}) == len(identities))


def valid_text_item(item):
    return (isinstance(item, dict) and set(item) == {'type', 'text'}
            and item['type'] == 'text' and isinstance(item['text'], str))


def payload_records(content):
    if not isinstance(content, list) or not content or not all(valid_text_item(i) for i in content):
        return None
    decoded = []
    for item in content:
        try:
            value = json.loads(item['text'])
        except ValueError:
            continue
        if isinstance(value, list):
            decoded.append(value)
    return decoded[0] if len(decoded) == 1 else None


def matches_records(content, records):
    parsed = payload_records(content)
    if not parsed:
        return False
    # Canonical JSON preserves JSON types: bool is never a native integer zero.
    return sorted(identity_key(r) for r in parsed) == sorted(identity_key(r) for r in records)


def batch_records(snapshot, identities):
    keys = {identity_key(i) for i in identities}
    return [r for r in snapshot['completions'] if identity_key(r['identity']) in keys]


def propose(snapshot, event):
    identities = event.get('completion_ids')
    if not nonempty(event.get('batch_id')) or not valid_members(identities):
        return False
    existing = find_batch(snapshot, event)
    if existing is not None:
        return existing['completion_ids'] == identities and existing['input'] == event.get('input')
    records = unassigned(snapshot)
    if not membership_matches(identities, records):
        return False
    if not current_revision(snapshot, event) or not matches_records(event.get('input'), records):
        return False
    snapshot['deliveries'].append(dict(batch_id=event['batch_id'], completion_ids=deepcopy(identities),
        input=deepcopy(event['input']), created_revision=snapshot['revision'] + 1, status='pending', attempts=[]))
    return True


def membership_matches(identities, records):
    return {identity_key(i) for i in identities} == {identity_key(r['identity']) for r in records}


def current_revision(snapshot, event):
    value = event.get('observed_revision')
    return type(value) is int and value == snapshot['revision']


def find_batch(snapshot, event):
    return next((b for b in snapshot['deliveries'] if b['batch_id'] == event.get('batch_id')), None)


def send_selection(snapshot, event):
    if event.get('thread_id') != snapshot['binding']['conversation_id']:
        return False
    main = snapshot['main']
    if event.get('method') == 'turn/steer':
        return main['status'] == 'active' and event.get('expected_turn_id') == main['turn_id']
    return start_selection(snapshot, event)


def start_selection(snapshot, event):
    main = snapshot['main']
    idle = event.get('selection') == dict(source='thread/read',
        thread_id=snapshot['binding']['conversation_id'], status='idle')
    if 'selection' in event and not idle:
        return False
    return (event.get('method') == 'turn/start' and event.get('expected_turn_id', '') is None
            and (main['status'] == 'stopped' or idle) and inputs_resolved(snapshot))


ATTEMPT_FIELDS = ('attempt_id', 'client_message_id', 'method', 'thread_id', 'expected_turn_id')


def same_attempt(attempt, event):
    return all(attempt[k] == event.get(k) for k in ATTEMPT_FIELDS)


def send(snapshot, event):
    batch = find_batch(snapshot, event)
    if batch is None:
        return False
    for attempt in batch['attempts']:
        if attempt['attempt_id'] == event.get('attempt_id'):
            return same_attempt(attempt, event)
    if not send_available(snapshot, batch, event):
        return False
    revision = snapshot['revision'] + 1
    attempt = {k: event[k] for k in ATTEMPT_FIELDS}
    attempt.update(admission_revision=revision, status='sent-unconfirmed', receipt=None, rejection=None)
    batch['attempts'].append(attempt)
    snapshot['inputs'][event['client_message_id']] = dict(status='pending', turn_id=None, revision=revision)
    return True


def send_available(snapshot, batch, event):
    if not all(nonempty(event.get(k)) for k in ('attempt_id', 'client_message_id')):
        return False
    attempts = [a for b in snapshot['deliveries'] for a in b['attempts']]
    return all((batch['status'] == 'pending',
                all(a['status'] == 'rejected' for a in batch['attempts']),
                all(a['attempt_id'] != event['attempt_id'] for a in attempts),
                event['client_message_id'] not in snapshot['inputs'],
                current_revision(snapshot, event), send_selection(snapshot, event)))


def known_rejection(attempt, rejection):
    if not isinstance(rejection, dict) or attempt['method'] != 'turn/steer':
        return False
    if rejection.get('kind') != 'expected-active-turn' or rejection.get('code') != -32600:
        return False
    message = rejection.get('message')
    if message == 'no active turn to steer':
        return True
    return mismatched_turn_message(message, attempt['expected_turn_id'])


def mismatched_turn_message(message, expected):
    if not isinstance(message, str):
        return False
    prefix = f"expected active turn id `{expected}` but found `"
    match = re.fullmatch(re.escape(prefix) + r'(.+)`', message)
    return match is not None and match.group(1) != expected


def receipt_for(attempt, event):
    turn = event.get('turn_id')
    if event.get('thread_id') != attempt['thread_id'] or not nonempty(turn):
        return None
    if attempt['method'] == 'turn/steer' and turn != attempt['expected_turn_id']:
        return None
    return dict(source='ack', thread_id=event['thread_id'], turn_id=turn, item_id=None)


def resolve(snapshot, event):
    batch = find_batch(snapshot, event)
    if batch is None:
        return False
    attempt = next((a for a in batch['attempts'] if a['attempt_id'] == event.get('attempt_id')), None)
    if attempt is None or attempt['client_message_id'] != event.get('client_message_id'):
        return False
    if event['type'] == 'delivery/ack':
        return acknowledge(snapshot, batch, attempt, event)
    if event['type'] == 'delivery/uncertain':
        return uncertain(snapshot, batch, attempt, event)
    return reject_uncertain(snapshot, batch, attempt, event)


def reject_uncertain(snapshot, batch, attempt, event):
    was_uncertain = attempt['status'] == 'sent-unconfirmed'
    accepted = reject(snapshot, attempt, event)
    if accepted and was_uncertain:
        resolve_condition(snapshot, batch['batch_id'])
    return accepted


def acknowledge(snapshot, batch, attempt, event):
    receipt = receipt_for(attempt, event)
    if receipt is None or attempt['status'] == 'rejected':
        return False
    if attempt['status'] == 'confirmed':
        return all(attempt['receipt'][k] == receipt[k] for k in ('thread_id', 'turn_id'))
    input_receipt = snapshot['inputs'][attempt['client_message_id']]
    turn = receipt['turn_id']
    settled = snapshot['main']['completed_turns'].get(turn, -1) > input_receipt['revision']
    input_receipt.update(status='settled' if settled else 'accepted', turn_id=turn)
    attempt.update(status='confirmed', receipt=receipt)
    batch['status'] = 'confirmed'
    resolve_condition(snapshot, batch['batch_id'])
    return True


def resolve_condition(snapshot, batch_id):
    for alert in snapshot['alerts']:
        if alert.get('kind') == 'delivery-uncertain' and alert.get('batch_id') == batch_id:
            alert['status'] = 'resolved'


def uncertain(snapshot, batch, attempt, event):
    if attempt['status'] != 'sent-unconfirmed' or not nonempty(event.get('message')):
        return False
    for alert in snapshot['alerts']:
        if alert.get('kind') == 'delivery-uncertain' and alert.get('batch_id') == batch['batch_id']:
            alert.update(status='pending', message=event['message'])
            return True
    snapshot['alerts'].append(dict(kind='delivery-uncertain', batch_id=batch['batch_id'],
                                  status='pending', message=event['message']))
    return True


def reject(snapshot, attempt, event):
    rejection = event.get('rejection')
    if not known_rejection(attempt, rejection) or attempt['status'] == 'confirmed':
        return False
    if attempt['status'] == 'rejected':
        return attempt['rejection'] == rejection
    attempt.update(status='rejected', rejection=deepcopy(rejection))
    snapshot['inputs'][attempt['client_message_id']]['status'] = 'rejected'
    return True


def apply_delivery(snapshot, event, state_dir):
    kind = event['type']
    if kind in ('delivery/proposed', 'delivery/sent'):
        if not gate_open(snapshot, state_dir):
            return False
        return propose(snapshot, event) if kind == 'delivery/proposed' else send(snapshot, event)
    if kind in ('delivery/ack', 'delivery/rejected', 'delivery/uncertain'):
        return resolve(snapshot, event)
    return False
