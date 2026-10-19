"""Streaming USB debug proxy. No bodies or credentials are logged/saved."""
import argparse
import http.client
import http.server
import select
import signal
import socket
import urllib.parse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', required=True, help='HTTP hostname to route to the local backend')
parser.add_argument('--proxy-port', type=int, default=18888)
parser.add_argument('--backend-port', type=int, default=18080)
parser.add_argument('--reject-path', help='Return 503 for this exact path until SIGUSR1')
args = parser.parse_args()
for port in (args.proxy_port, args.backend_port):
    if not 1 <= port <= 65535:
        parser.error('ports must be between 1 and 65535')
if args.reject_path is not None and not args.reject_path.startswith('/'):
    parser.error('--reject-path must start with /')
DEV_HOST = args.host.lower()
UPLOAD = args.reject_path
reject_uploads = UPLOAD is not None


def enable_uploads(*_):
    global reject_uploads
    reject_uploads = False
    print('MODE requests -> local backend', flush=True)


signal.signal(signal.SIGUSR1, enable_uploads)


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
        url = urllib.parse.urlsplit(self.path)
        host = url.hostname or self.headers.get('Host', '').split(':')[0]
        port = url.port or 80
        path = urllib.parse.urlunsplit(('', '', url.path or '/', url.query, ''))
        local = host == DEV_HOST
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

        is_upload = local and url.path == UPLOAD
        if is_upload and reject_uploads:
            for _ in body():
                pass
            self.send_response(503)
            self.send_header('Content-Length', '0')
            self.end_headers()
            print(f'UPLOAD injected_status=503 length={length} session_present={"Session-Id" in self.headers}', flush=True)
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('proxy-connection', 'connection', 'transfer-encoding')}
        headers['Connection'] = 'close'
        connection = http.client.HTTPConnection('127.0.0.1' if local else host, args.backend_port if local else port, timeout=120)
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
                print(f'LOCAL {self.command} {url.path} status={response.status} length={length} session_present={"Session-Id" in self.headers}', flush=True)
        except (OSError, EOFError, http.client.HTTPException) as error:
            print(f'PROXY_ERROR {type(error).__name__} local={local}', flush=True)
            self.close_connection = True
        finally:
            connection.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = forward


print(f'Listening 127.0.0.1:{args.proxy_port}; {DEV_HOST} -> 127.0.0.1:{args.backend_port}; reject_path={UPLOAD!r}', flush=True)
http.server.ThreadingHTTPServer(('127.0.0.1', args.proxy_port), Proxy).serve_forever()
