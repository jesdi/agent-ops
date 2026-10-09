"""Validate persisted batches against immutable completions and input provenance."""
from dispatcher.runtime_work import nonempty, identity_key
from dispatcher.runtime_delivery import valid_members, batch_records, matches_records, known_rejection


def bounded_revision(value, snapshot, minimum=0):
    return type(value) is int and minimum <= value <= snapshot['revision']


def valid_deliveries(snapshot):
    batches = snapshot.get('deliveries')
    if not isinstance(batches, list):
        return False
    if not all(valid_batch(b, snapshot) for b in batches):
        return False
    return unique_assignments(batches)


def unique(values):
    return len(values) == len(set(values))


def unique_assignments(batches):
    return all((unique([b['batch_id'] for b in batches]),
        unique([identity_key(i) for b in batches for i in b['completion_ids']]),
        unique([a['attempt_id'] for b in batches for a in b['attempts']]),
        unique([a['client_message_id'] for b in batches for a in b['attempts']])))


def valid_batch(batch, snapshot):
    if not isinstance(batch, dict) or not valid_members(batch.get('completion_ids')):
        return False
    records = batch_records(snapshot, batch['completion_ids'])
    return all((nonempty(batch.get('batch_id')), len(records) == len(batch['completion_ids']),
        matches_records(batch.get('input'), records),
        bounded_revision(batch.get('created_revision'), snapshot),
        valid_batch_attempts(batch, snapshot)))


def valid_batch_attempts(batch, snapshot):
    attempts = batch.get('attempts')
    if not isinstance(attempts, list) or batch.get('status') not in ('pending', 'confirmed'):
        return False
    if not all(valid_attempt(a, snapshot, batch['created_revision']) for a in attempts):
        return False
    statuses = [a['status'] for a in attempts]
    return (all(s == 'rejected' for s in statuses[:-1])
            and (batch['status'] == 'confirmed') == ('confirmed' in statuses))


def valid_attempt(attempt, snapshot, created):
    if not isinstance(attempt, dict):
        return False
    if not all(nonempty(attempt.get(k)) for k in ('attempt_id', 'client_message_id')):
        return False
    if not bounded_revision(attempt.get('admission_revision'), snapshot, max(1, created)):
        return False
    return valid_target(attempt, snapshot) and valid_attempt_input(attempt, snapshot)


def valid_target(attempt, snapshot):
    if attempt.get('thread_id') != snapshot['binding']['conversation_id']:
        return False
    if attempt.get('method') == 'turn/steer':
        return nonempty(attempt.get('expected_turn_id'))
    return attempt.get('method') == 'turn/start' and attempt.get('expected_turn_id', '') is None


def valid_attempt_input(attempt, snapshot):
    inputs = snapshot.get('inputs')
    if not isinstance(inputs, dict):
        return False
    receipt = inputs.get(attempt['client_message_id'])
    if not isinstance(receipt, dict) or receipt.get('revision') != attempt['admission_revision']:
        return False
    status = attempt.get('status')
    if status == 'confirmed':
        return valid_confirmation(attempt, receipt)
    if status == 'rejected':
        return (receipt.get('status') == 'rejected' and attempt.get('receipt', '') is None
                and known_rejection(attempt, attempt.get('rejection')))
    return valid_pending(attempt, receipt)


def valid_pending(attempt, receipt):
    return all((attempt.get('status') == 'sent-unconfirmed', receipt.get('status') == 'pending',
                attempt.get('receipt', '') is None, attempt.get('rejection', '') is None))


def valid_confirmation(attempt, input_receipt):
    receipt = attempt.get('receipt')
    if not isinstance(receipt, dict) or attempt.get('rejection', '') is not None:
        return False
    return all((receipt.get('source') == 'ack', receipt.get('item_id', '') is None,
        receipt.get('thread_id') == attempt['thread_id'], nonempty(receipt.get('turn_id')),
        receipt.get('turn_id') == input_receipt.get('turn_id'),
        input_receipt.get('status') in ('accepted', 'settled'),
        attempt['method'] != 'turn/steer' or receipt.get('turn_id') == attempt['expected_turn_id']))
