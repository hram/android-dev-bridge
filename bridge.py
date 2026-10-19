"""Shared streaming proxy runtime. No Android settings are modified here."""
import http.client
import http.server
import select
import socket
import urllib.parse
import threading
import uuid
from collections import deque
from datetime import datetime, timezone


class BridgeError(ValueError):
    pass


def validate_path(path):
    if path is not None and (not path.startswith('/') or '?' in path or '#' in path or len(path) > 2048):
        raise BridgeError('reject_path must be an absolute path without query or fragment')


class ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        client, address = super().get_request()
        client.settimeout(120)
        with self.bridge.lock:
            self.bridge.connections.add(client)
        return client, address

    def close_request(self, client):
        with self.bridge.lock:
            self.bridge.connections.discard(client)
        super().close_request(client)

    def handle_error(self, request, client_address):
        # Never dump request data or exception tracebacks into MCP stdout/stderr.
        pass


class Bridge:
    def __init__(self, on_event=None):
        self.lock = threading.RLock()
        self.server = None
        self.thread = None
        self.connections = set()
        self.events = deque(maxlen=500)
        self.sequence = 0
        self.session_id = None
        self.host = None
        self.backend_port = None
        self.reject_path = None
        self.on_event = on_event

    def start(self, host, proxy_port=18888, backend_port=18080, reject_path=None):
        host = host.lower().strip()
        if not host or len(host) > 253 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in host):
            raise BridgeError('host must be a hostname without scheme, path or port')
        for port in (proxy_port, backend_port):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise BridgeError('ports must be between 1 and 65535')
        validate_path(reject_path)
        with self.lock:
            if self.server is not None:
                raise BridgeError('A proxy is already running; stop it before starting another')
            server = ProxyServer(('127.0.0.1', proxy_port), Proxy, bind_and_activate=False)
            server.bridge = self
            try:
                server.server_bind()
                server.server_activate()
            except OSError:
                server.server_close()
                raise BridgeError('Cannot bind proxy port') from None
            self.host, self.backend_port, self.reject_path = host, backend_port, reject_path
            self.events.clear()
            self.session_id = str(uuid.uuid4())
            self.server = server
            self.thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
            self.thread.start()
            return self.status()

    def status(self):
        with self.lock:
            return {'running': self.server is not None, 'session_id': self.session_id,
                    'host': self.host, 'proxy_port': self.server.server_port if self.server else None,
                    'backend_host': '127.0.0.1', 'backend_port': self.backend_port,
                    'reject_path': self.reject_path, 'retained_requests': len(self.events),
                    'last_sequence': self.sequence, 'device_setup': 'manual'}

    def require_session(self, session_id):
        if self.server is None or session_id != self.session_id:
            raise BridgeError('No running proxy with this session_id')

    def set_fault(self, session_id, reject_path=None):
        validate_path(reject_path)
        with self.lock:
            self.require_session(session_id)
            self.reject_path = reject_path
            return self.status()

    def record(self, method, path, status, length, session_present, injected=False, error=None, session_id=None):
        with self.lock:
            if session_id is not None and session_id != self.session_id:
                return
            self.sequence += 1
            event = {'sequence': self.sequence, 'session_id': self.session_id,
                     'at': datetime.now(timezone.utc).isoformat(), 'method': method,
                     'path': path, 'status': status, 'content_length': length,
                     'session_present': session_present, 'injected': injected, 'error': error}
            self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def requests(self, after=0, limit=50):
        if after < 0 or not 1 <= limit <= 500:
            raise BridgeError('after must be >= 0; limit must be between 1 and 500')
        with self.lock:
            matches = [dict(e) for e in self.events if e['sequence'] > after]
            page = matches[:limit]
            return {'requests': page, 'next_after': page[-1]['sequence'] if page else after,
                    'has_more': len(matches) > limit,
                    'truncated': bool(self.events and after < self.events[0]['sequence'] - 1)}

    def stop(self, session_id):
        with self.lock:
            self.require_session(session_id)
            server = self.server
        server.shutdown()
        with self.lock:
            for client in list(self.connections):
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.close()
            server.server_close()
            self.thread.join(timeout=2)
            self.server = None
            self.reject_path = None
            return self.status()


class Proxy(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        host, port = self.path.rsplit(':', 1)
        try:
            with socket.create_connection((host, int(port)), timeout=30) as upstream:
                self.send_response(200)
                self.end_headers()
                while True:
                    ready, _, _ = select.select([self.connection, upstream], [], [], 90)
                    if not ready:
                        break
                    for source in ready:
                        block = source.recv(65536)
                        if not block:
                            return
                        (upstream if source is self.connection else self.connection).sendall(block)
        except OSError:
            return

    def forward(self):
        with self.server.bridge.lock:
            target_host = self.server.bridge.host
            backend_port = self.server.bridge.backend_port
            reject_path = self.server.bridge.reject_path
            session_id = self.server.bridge.session_id
        url = urllib.parse.urlsplit(self.path)
        host = url.hostname or self.headers.get('Host', '').split(':')[0]
        port = url.port or 80
        path = urllib.parse.urlunsplit(('', '', url.path or '/', url.query, ''))
        local = host.lower() == target_host
        length = int(self.headers.get('Content-Length', '0'))
        chunked = self.headers.get('Transfer-Encoding', '').lower() == 'chunked'

        def body():
            if chunked:
                while True:
                    size = int(self.rfile.readline().split(b';')[0], 16)
                    if not size:
                        while self.rfile.readline().strip():
                            pass
                        return
                    remaining = size
                    while remaining:
                        block = self.rfile.read(min(65536, remaining))
                        if not block:
                            raise EOFError('request truncated')
                        remaining -= len(block)
                        yield block
                    self.rfile.read(2)
            else:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(65536, remaining))
                    if not block:
                        raise EOFError('request truncated')
                    remaining -= len(block)
                    yield block

        is_upload = local and url.path == reject_path
        if is_upload and reject_path is not None:
            for _ in body():
                pass
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
            self.server.bridge.record(self.command, url.path, 503, length, "Session-Id" in self.headers, True, session_id=session_id)
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('proxy-connection', 'connection', 'transfer-encoding')}
        headers['Connection'] = 'close'
        connection = http.client.HTTPConnection('127.0.0.1' if local else host, backend_port if local else port, timeout=120)
        try:
            connection.request(self.command, path, body=body() if length or chunked else None, headers=headers, encode_chunked=chunked)
            response = connection.getresponse()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ('transfer-encoding', 'connection'):
                    self.send_header(key, value)
            self.end_headers()
            while block := response.read(65536):
                self.wfile.write(block)
            if local:
                self.server.bridge.record(self.command, url.path, response.status, length, "Session-Id" in self.headers, session_id=session_id)
        except (OSError, EOFError, http.client.HTTPException) as error:
            if local:
                self.server.bridge.record(self.command, url.path, None, length, "Session-Id" in self.headers, error=type(error).__name__, session_id=session_id)
            self.close_connection = True
        finally:
            connection.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = forward
