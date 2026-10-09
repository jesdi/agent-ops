"""Author regressions at the approved public runtime event/view boundary."""
from t6_locked.contract_fixture import ContractFixture


def test_contradictory_native_mismatch_cannot_release_a_pending_result():
    fixture = ContractFixture()
    try:
        fixture.completion()
        fixture.active('main-b')
        fixture.propose()
        fixture.send()
        before = fixture.view()
        accepted = fixture.control.event(fixture.binding, {
            'type': 'delivery/rejected', 'batch_id': 'batch-generic',
            'attempt_id': 'attempt-generic', 'client_message_id': 'client-generic',
            'rejection': {'kind': 'expected-active-turn', 'code': -32600,
                          'message': 'expected active turn id `main-b` but found `main-b`'},
        })
        assert accepted is False
        assert fixture.view() == before
    finally:
        fixture.close()


from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import socket
import socketserver
import threading
import unittest

from dispatcher.runtime_http import UnixHTTP
from t6_locked.external_fixture import ExternalFixture, eventually


@contextmanager
def lose_sent_response(state):
    """An owned wire proxy loses only the first committed send response."""
    path = state / 'wait/wait.sock'
    upstream = state / 'wait/actual.sock'
    path.rename(upstream)
    lost = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            packet = json.loads(body)
            connection = UnixHTTP(state)
            connection.path = str(upstream)
            try:
                connection.request('POST', self.path, body, dict(self.headers))
                response = connection.getresponse()
                data = response.read()
                if packet.get('event', {}).get('type') == 'delivery/sent' and not lost.is_set():
                    assert response.status == 200 and json.loads(data) is True
                    lost.set()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                connection.close()

    server = socketserver.UnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        path.unlink(missing_ok=True)
        upstream.rename(path)


class ListenerResponseLossTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_committed_send_response_never_forwards_or_retries(self):
        flow = ExternalFixture(name='author-lost-listener-response')
        self.addAsyncCleanup(flow.close)
        await flow.start()
        with lose_sent_response(flow.state) as lost:
            command = await flow.command()
            await flow.qualify([command])
            identity = await flow.finish(command)
            await eventually(lost.is_set, 'listener never committed the physical attempt')
            before = flow.view()
            batch = next(b for b in before['deliveries'] if identity in b['completion_ids'])
            attempt = batch['attempts'][0]
            assert attempt['status'] == 'sent-unconfirmed'
            assert before['inputs'][attempt['client_message_id']]['status'] == 'pending'
            polls = sum(r['kind'] == 'client-request' and r['payload']['method'] ==
                        'thread/backgroundTerminals/list' for r in flow.records())
            await eventually(lambda: sum(r['kind'] == 'client-request' and r['payload']['method'] ==
                'thread/backgroundTerminals/list' for r in flow.records()) > polls + 2,
                'inventory stopped while a listener response was uncertain')
            assert flow.view()['deliveries'] == before['deliveries']
            assert flow.result_requests() == []
