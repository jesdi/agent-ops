"""Real supervisor recovery holds ambiguous full history without resending input."""
from copy import deepcopy

import pytest

from t5_observed_session import ObservedSession
from test_t6_crap_regressions import publish_task, qualify_command, finish_command, result_requests


@pytest.mark.parametrize('missing_item', [True, False], ids=['absent-item', 'different-item'])
def test_completed_history_needs_the_same_item_before_receipt_local_settlement(monkeypatch, missing_item):
    with ObservedSession(monkeypatch) as session:
        identity = session.view()['bootstrap']['initial_input']['client_message_id']
        assert session.until(lambda: session.view()['inputs'][identity]['status'] == 'accepted')
        before = session.view()
        thread = deepcopy(session.backend('snapshot')['threads'][session.root])
        turn = thread['turns'][-1]
        turn.update(status='completed', error=None)
        thread['status'] = {'type': 'idle'}
        damaged = deepcopy(thread)
        if missing_item:
            damaged['turns'][-1]['items'] = []
        else:
            user = next(item for item in damaged['turns'][-1]['items'] if item['type'] == 'userMessage')
            user['id'] = 'different-item-generic'
        session.backend('fault_next', matches={'method': 'thread/read', 'threadId': session.root},
                        fault={'result': {'thread': damaged}}, times=1000)
        session.backend('set_thread', thread_id=session.root, overrides=thread)
        assert session.until(lambda: sum(row['kind'] == 'rpc_response' and row['method'] == 'thread/read'
            and row['packet'].get('result') == {'thread': damaged} for row in session.rows()) >= 4)
        held = session.view()
        assert held['inputs'][identity]['status'] == 'accepted'
        assert held['main'] == before['main']
        session.backend('clear_faults')
        assert session.until(lambda: session.view()['inputs'][identity]['status'] == 'settled')
        settled = session.view()
        assert settled['inputs'][identity]['history_settlement']['turn_id'] == turn['id']
        assert settled['main'] == before['main']
        assert len([row for row in session.rows() if row['kind'] == 'rpc_received'
                    and row['method'] == 'turn/start'
                    and row['params'].get('clientUserMessageId') == identity]) == 1


def test_uncertain_delivery_repolls_without_resend_then_history_confirms_same_attempt(monkeypatch):
    with ObservedSession(monkeypatch) as session:
        publish_task(session)
        qualify_command(session)
        session.backend('fault_next', matches={'method': 'turn/start', 'threadId': session.root},
                        fault={'result': None})
        session.backend('fault_next', matches={'method': 'thread/items/list', 'threadId': session.root},
                        fault={'result': {'data': [], 'nextCursor': None}}, times=1000)
        finish_command(session)
        assert session.until(lambda: any(a.get('kind') == 'delivery-uncertain'
            and a['status'] == 'pending' for a in session.view()['alerts']))
        before = session.view()
        batch = before['deliveries'][0]
        attempts = deepcopy(batch['attempts'])
        assert len(attempts) == 1
        assert attempts[0]['status'] == 'sent-unconfirmed'
        scans = sum(row['kind'] == 'rpc_received' and row['method'] == 'thread/read' for row in session.rows())
        assert session.until(lambda: sum(row['kind'] == 'rpc_received' and row['method'] == 'thread/read'
            for row in session.rows()) >= scans + 4)
        assert session.view()['deliveries'] == before['deliveries']
        assert len(result_requests(session)) == 1
        session.backend('clear_faults')
        assert session.until(lambda: session.view()['deliveries'][0]['status'] == 'confirmed')
        after = session.view()
        confirmed = after['deliveries'][0]
        assert confirmed['input'] == batch['input']
        assert len(confirmed['attempts']) == 1
        assert confirmed['attempts'][0]['attempt_id'] == attempts[0]['attempt_id']
        assert confirmed['attempts'][0]['admission_revision'] == attempts[0]['admission_revision']
        assert confirmed['attempts'][0]['receipt']['source'] == 'history'
        assert after['alerts'][0]['status'] == 'resolved'
        assert len(result_requests(session)) == 1
