"""Receipt-local acceptance and normal-own-end proof, without lifecycle authority."""
from dispatcher.runtime_input import input_sources, matches_input, valid_native_input
from dispatcher.runtime_work import nonempty


def valid_scan(scan, root, turn):
    if not isinstance(scan, dict):
        return False
    return all((scan.get('thread_id') == root, scan.get('turn_id', '') in (None, turn),
                scan.get('sort_direction') in ('asc', 'desc'), scan.get('from_cursor', '') is None,
                scan.get('final_cursor', '') is None, scan.get('complete') is True))


def matching_receipt(snapshot, event):
    identity = event.get('client_message_id')
    if not nonempty(identity):
        return None
    receipt = snapshot['inputs'].get(identity)
    if receipt is None or receipt['status'] == 'rejected':
        return None
    if event.get('thread_id') != snapshot['binding']['conversation_id']:
        return None
    if not all(nonempty(event.get(k)) for k in ('turn_id', 'item_id')):
        return None
    if receipt['turn_id'] not in (None, event['turn_id']):
        return None
    return receipt


def same_identity(receipt, event):
    return all(receipt.get(k) == event.get(k) for k in ('thread_id', 'turn_id', 'item_id'))


def accept_history(snapshot, event):
    receipt = matching_receipt(snapshot, event)
    if receipt is None or not valid_scan(event.get('scan'), event['thread_id'], event['turn_id']):
        return False
    if not matches_input(snapshot, event['client_message_id'], event['turn_id'], event.get('input')):
        return False
    previous = receipt.get('history_receipt')
    if previous is not None:
        return same_identity(previous, event)
    proof = {k: event[k] for k in ('thread_id', 'turn_id', 'item_id')}
    receipt['history_receipt'] = dict(proof, revision=snapshot['revision'] + 1)
    turn = event['turn_id']
    settled = snapshot['main']['completed_turns'].get(turn, -1) > receipt['revision']
    receipt.update(status='settled' if settled else 'accepted', turn_id=turn)
    confirm_batch(snapshot, event['client_message_id'], proof)
    return True


def confirm_batch(snapshot, identity, proof):
    from dispatcher.runtime_delivery import resolve_condition
    for batch in snapshot['deliveries']:
        for attempt in batch['attempts']:
            if attempt['client_message_id'] != identity:
                continue
            if attempt['status'] != 'confirmed':
                attempt.update(status='confirmed', receipt=dict(proof, source='history'))
            batch['status'] = 'confirmed'
            resolve_condition(snapshot, batch['batch_id'])


def normal_end_matches(snapshot, event):
    end = event.get('normal_end')
    if not isinstance(end, dict):
        return False
    if not all((end.get('status') == 'completed', end.get('error', '') is None,
                end.get('items_view') == 'full', end.get('source') in ('thread/read', 'thread/turns/list'))):
        return False
    if end['source'] == 'thread/turns/list' and not valid_scan(end.get('turn_scan'), event['thread_id'], None):
        return False
    item = end.get('matching_item')
    if not isinstance(item, dict):
        return False
    return all((item.get('type') == 'userMessage', item.get('id') == event['item_id'],
        item.get('client_id') == event['client_message_id'],
        matches_input(snapshot, event['client_message_id'], event['turn_id'], item.get('input'))))


def settle_history(snapshot, event):
    receipt = matching_receipt(snapshot, event)
    if receipt is None or not same_identity(receipt.get('history_receipt', {}), event):
        return False
    if not normal_end_matches(snapshot, event):
        return False
    if receipt['status'] == 'settled':
        return True
    receipt['history_settlement'] = dict(
        kind='accepted-input-in-normal-completed-turn', evidence_source='authoritative-root-history',
        thread_id=event['thread_id'], turn_id=event['turn_id'], item_id=event['item_id'],
        admission_revision=receipt['revision'], receipt_revision=receipt['history_receipt']['revision'],
        revision=snapshot['revision'] + 1)
    receipt['status'] = 'settled'
    return True


def valid_extensions(snapshot, identity, receipt):
    root = snapshot['binding']['conversation_id']
    if 'native_input' in receipt and not valid_native_input(receipt['native_input'], root):
        return False
    history = receipt.get('history_receipt')
    if history is not None and not valid_history(snapshot, identity, receipt, history):
        return False
    if 'history_settlement' in receipt:
        return valid_settlement(receipt, snapshot['revision'])
    return True


def valid_history(snapshot, identity, receipt, history):
    if not isinstance(history, dict):
        return False
    revision = history.get('revision')
    return all((history.get('thread_id') == snapshot['binding']['conversation_id'],
        nonempty(history.get('item_id')), history.get('turn_id') == receipt['turn_id'],
        receipt['status'] in ('accepted', 'settled'),
        type(revision) is int and receipt['revision'] < revision <= snapshot['revision'],
        bool(input_sources(snapshot, identity))))


def valid_settlement(receipt, revision):
    proof = receipt.get('history_settlement')
    history = receipt.get('history_receipt')
    if not isinstance(proof, dict) or not isinstance(history, dict):
        return False
    recorded = proof.get('revision')
    return all((receipt['status'] == 'settled', same_identity(proof, history),
        proof.get('kind') == 'accepted-input-in-normal-completed-turn',
        proof.get('evidence_source') == 'authoritative-root-history',
        proof.get('admission_revision') == receipt['revision'],
        proof.get('receipt_revision') == history['revision'],
        type(recorded) is int and receipt['revision'] < history['revision'] <= recorded <= revision))
