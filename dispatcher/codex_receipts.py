"""Recover immutable input receipts from independently exhausted native item scopes."""
from dispatcher.codex_inventory import native_item, native_thread, native_turn, pages, require
from dispatcher.runtime_input import input_sources, matches_input


def scan_scope(root):
    return dict(thread_id=root, turn_id=None, sort_direction='asc',
                from_cursor=None, final_cursor=None, complete=True)


def unique_message(entries, identity):
    observed = {}
    for entry in entries:
        item = entry['item']
        if item['type'] != 'userMessage' or item.get('clientId') != identity:
            continue
        key = item['id']
        require(key not in observed or observed[key] == entry, 'Conflicting repeated native receipt')
        observed[key] = entry
    return next(iter(observed.values())) if len(observed) == 1 else None


def receipt_event(snapshot, identity, entries):
    entry = unique_message(entries, identity)
    if entry is None:
        return None
    item, turn = entry['item'], entry['turnId']
    if not matches_input(snapshot, identity, turn, item.get('content')):
        return None
    root = snapshot['binding']['conversation_id']
    return dict(type='input/history', client_message_id=identity, thread_id=root,
                turn_id=turn, item_id=item['id'], input=item['content'], scan=scan_scope(root))


def settlement_event(event, thread):
    turns = [turn for turn in thread['turns'] if turn['id'] == event['turn_id']]
    if len(turns) != 1:
        return None
    turn = turns[0]
    if not all((turn.get('status') == 'completed', turn.get('error', '') is None,
                turn.get('itemsView') == 'full')):
        return None
    entries = [dict(turnId=turn['id'], item=item) for item in turn['items']]
    match = unique_message(entries, event['client_message_id'])
    if match is None or match['item']['id'] != event['item_id']:
        return None
    item = match['item']
    return dict(type='input/history-settled', client_message_id=event['client_message_id'],
        thread_id=event['thread_id'], turn_id=event['turn_id'], item_id=event['item_id'],
        normal_end=dict(status='completed', error=None, items_view='full', source='thread/read',
            matching_item=dict(type='userMessage', id=item['id'], client_id=item['clientId'], input=item.get('content'))))


async def reconcile(rpc, snapshot, emit):
    identities = [identity for identity, receipt in snapshot['inputs'].items()
                  if receipt['status'] in ('pending', 'accepted') and input_sources(snapshot, identity)]
    if not identities:
        return
    root = snapshot['binding']['conversation_id']
    entries = await pages(rpc, 'thread/items/list', dict(threadId=root, sortDirection='asc'), native_item)
    response = await rpc.call('thread/read', dict(threadId=root, includeTurns=True))
    require(isinstance(response, dict), 'Unreadable receipt thread')
    thread = response.get('thread')
    require(native_thread(thread) and thread['id'] == root, 'Foreign or unreadable receipt thread')
    require(all(native_turn(turn) for turn in thread['turns']), 'Unreadable receipt turns')
    for identity in identities:
        await reconcile_receipt(snapshot, identity, entries, thread, emit)


async def reconcile_receipt(snapshot, identity, entries, thread, emit):
    event = receipt_event(snapshot, identity, entries)
    if event is not None and await emit(event):
        settlement = settlement_event(event, thread)
        if settlement is not None:
            await emit(settlement)
