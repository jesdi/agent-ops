"""Prompt same-root delivery, with independent replies outside the input lock."""
import asyncio
import json
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from dispatcher.codex_transport import ProtocolError
from dispatcher.codex_inventory import native_thread, native_turn, require
from dispatcher.runtime_delivery import unassigned, known_rejection
from dispatcher.runtime_snapshots import valid_snapshot


def selection(response, root):
    require(isinstance(response, dict), 'Unreadable thread/read result')
    thread = response.get('thread')
    if not native_thread(thread) or thread['id'] != root:
        return None
    turns = thread['turns']
    if not all(native_turn(t) for t in turns):
        return None
    status = thread['status']['type']
    if status == 'active':
        return active_selection(turns)
    if status == 'idle' and all(t['status'] != 'inProgress' for t in turns):
        return 'turn/start', None
    return None


def active_selection(turns):
    active = [t for t in turns if t['status'] == 'inProgress']
    if len(active) != 1 or turns[-1] != active[0]:
        return None
    return 'turn/steer', active[0]['id']


def response_event(attempt, packet):
    base = {k: attempt[k] for k in ('batch_id', 'attempt_id', 'client_message_id')}
    error = packet.get('error')
    if isinstance(error, dict):
        rejection = dict(kind='expected-active-turn', code=error.get('code'), message=error.get('message'))
        if known_rejection(attempt, rejection):
            return dict(base, type='delivery/rejected', rejection=rejection)
        return None
    turn = accepted_turn(attempt, packet.get('result'))
    if turn is None:
        return None
    return dict(base, type='delivery/ack', thread_id=attempt['thread_id'], turn_id=turn)


def accepted_turn(attempt, result):
    if not isinstance(result, dict):
        return None
    if attempt['method'] == 'turn/steer':
        return result.get('turnId') if result.get('turnId') == attempt['expected_turn_id'] else None
    turn = result.get('turn')
    return turn['id'] if native_turn(turn) else None


class DeliveryPump:
    def __init__(self, client, binding, input_lock):
        self.client, self.binding, self.input_lock = client, binding, input_lock
        self.pending = set()

    async def view(self):
        return await asyncio.to_thread(self.client.view)

    async def event(self, event):
        try:
            return await asyncio.to_thread(self.client.event, self.binding, event) is True
        except (OSError, RuntimeError, ValueError):
            # Durable uncertainty never authorizes transmission or repetition.
            return False

    async def poll(self, rpc):
        async with self.input_lock:
            snapshot = await self.view()
            if not valid_snapshot(snapshot) or snapshot['retired']:
                return
            records = unassigned(snapshot)
            if records:
                await self.propose(snapshot, records)
                snapshot = await self.view()
            for batch in snapshot['deliveries']:
                if batch['status'] == 'pending' and all(a['status'] == 'rejected' for a in batch['attempts']):
                    await self.forward(rpc, batch)

    async def propose(self, snapshot, records):
        return await self.event(dict(type='delivery/proposed', observed_revision=snapshot['revision'],
            batch_id=str(uuid4()), completion_ids=[r['identity'] for r in records],
            input=[dict(type='text', text=json.dumps(records, ensure_ascii=False))]))

    async def forward(self, rpc, batch):
        root = self.binding['conversation_id']
        chosen = selection(await rpc.call('thread/read', dict(threadId=root, includeTurns=True)), root)
        if chosen is None:
            return
        method, turn = chosen
        if method == 'turn/steer':
            await self.event(dict(type='turn/recovered', thread_id=root, turn_id=turn, status='inProgress'))
        snapshot = await self.view()
        attempt = dict(type='delivery/sent', observed_revision=snapshot['revision'], batch_id=batch['batch_id'],
            attempt_id=str(uuid4()), client_message_id=str(uuid4()), method=method,
            thread_id=root, expected_turn_id=turn)
        if not await self.event(attempt):
            return
        params = dict(threadId=root, clientUserMessageId=attempt['client_message_id'], input=batch['input'])
        if method == 'turn/steer':
            params['expectedTurnId'] = turn
        request = await rpc.begin(method, params)
        task = asyncio.create_task(self.receipt(rpc, request, attempt))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def receipt(self, rpc, request, attempt):
        try:
            event = response_event(attempt, await rpc.finish(request))
            if event is not None:
                await self.event(event)
        except (OSError, ConnectionError, ConnectionClosed, TimeoutError, ProtocolError, ValueError, KeyError, TypeError):
            # Persisted sent-unconfirmed is retained, including after connection loss.
            return

    async def close(self):
        for task in self.pending:
            task.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
