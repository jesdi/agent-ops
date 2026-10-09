"""One-shot wire faults against an owned listener; durable bytes remain untouched."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import json
import socket
import socketserver
import threading

from dispatcher.runtime_http import UnixHTTP

@contextmanager
def view_fault_proxy(flow, mode):
    path = flow.state / 'wait/wait.sock'
    upstream = flow.state / 'wait/own-review-upstream.sock'
    path.rename(upstream)
    fired = threading.Event()
    armed = False
    evidence = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            nonlocal armed
            body = self.rfile.read(int(self.headers['Content-Length']))
            packet = json.loads(body)
            connection = UnixHTTP(flow.state)
            connection.path = str(upstream)
            try:
                connection.request('POST', self.path, body, dict(self.headers))
                response = connection.getresponse()
                data = response.read()
                if packet.get('event', {}).get('type') == 'delivery/proposed' and json.loads(data) is True:
                    armed = True
                    evidence.append({'route': self.path, 'request': packet, 'response': json.loads(data)})
                elif self.path == '/runtime/view' and armed and not fired.is_set():
                    evidence.append({'route': self.path, 'original_response': json.loads(data), 'fault_mode': mode})
                    fired.set()
                    if mode == 'drop':
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    data = json.dumps(None if mode == 'null' else {}).encode()
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally: connection.close()

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield fired, evidence
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert not thread.is_alive()
        path.unlink(missing_ok=True); upstream.rename(path)

