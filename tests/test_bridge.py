import asyncio
import http.client
import http.server
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import unittest
from bridge import Bridge, BridgeError

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Backend(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get('Transfer-Encoding') == 'chunked':
            chunks = []
            while True:
                size = int(self.rfile.readline(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            body = b''.join(chunks)
        else:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def request(port, path='/upload', chunked=False):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        body = iter([b'private-body', b'-end']) if chunked else b'private-body-end'
        conn.request('POST', 'http://api.example.test' + path, body=body,
                     headers={'Session-Id': 'private-session'}, encode_chunked=chunked)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.bridge = Bridge()

    def tearDown(self):
        if self.bridge.status()['running']:
            self.bridge.stop(self.bridge.status()['session_id'])
        self.backend.shutdown()
        self.backend.server_close()

    def start(self, **kwargs):
        return self.bridge.start('api.example.test', free_port(), self.backend.server_port, **kwargs)

    def test_forward_streaming_and_metadata_privacy(self):
        state = self.start()
        for chunked in (False, True):
            self.assertEqual(request(state['proxy_port'], '/upload?secret=query', chunked),
                             (200, b'private-body-end'))
        events = self.bridge.requests()
        self.assertEqual(len(events['requests']), 2)
        self.assertTrue(events['requests'][0]['session_present'])
        self.assertEqual(events['requests'][0]['path'], '/upload')
        for secret in ('private-body', 'private-session', 'secret=query'):
            self.assertNotIn(secret, json.dumps(events))

    def test_fault_lifecycle_and_session_identity(self):
        state = self.start(reject_path='/upload')
        self.assertEqual(request(state['proxy_port'], '/upload?x=y')[0], 503)
        self.assertEqual(request(state['proxy_port'], '/other')[0], 200)
        with self.assertRaises(BridgeError):
            self.bridge.set_fault('another-session')
        self.bridge.set_fault(state['session_id'])
        self.assertEqual(request(state['proxy_port'])[0], 200)
        self.assertFalse(self.bridge.stop(state['session_id'])['running'])
        second = self.bridge.start('api.example.test', state['proxy_port'], self.backend.server_port)
        self.assertNotEqual(second['session_id'], state['session_id'])
        with self.assertRaises(BridgeError):
            self.bridge.stop(state['session_id'])

    def test_bind_conflicts_and_validation_do_not_start_proxy(self):
        for host in ('http://api.example.test', 'api.test/path', 'api.test:80', ''):
            with self.assertRaises(BridgeError):
                self.bridge.start(host)
        with self.assertRaises(BridgeError):
            self.bridge.start('api.test', 0)
        with self.assertRaises(BridgeError):
            self.start(reject_path='/upload?token=secret')
        with self.assertRaises(BridgeError):
            self.bridge.start('api.test', self.backend.server_port)
        self.assertFalse(self.bridge.status()['running'])
        self.start()
        with self.assertRaises(BridgeError):
            self.start()

    def test_bounded_events_paging(self):
        for i in range(503):
            self.bridge.record('GET', '/path', 200, 0, False)
        page = self.bridge.requests(limit=2)
        self.assertTrue(page['truncated'])
        self.assertTrue(page['has_more'])
        self.assertEqual([e['sequence'] for e in page['requests']], [4, 5])
        self.assertEqual(self.bridge.requests(after=page['next_after'], limit=1)['requests'][0]['sequence'], 6)
        with self.assertRaises(BridgeError):
            self.bridge.requests(limit=501)

    def test_cli_signal_and_cleanup(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, str(ROOT/'proxy.py'), '--host', 'api.example.test',
                                 '--proxy-port', str(port), '--backend-port', str(self.backend.server_port),
                                 '--reject-path', '/upload'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            state = json.loads(proc.stderr.readline())
            self.assertEqual(request(port)[0], 503)
            if hasattr(signal, 'SIGUSR1'):
                proc.send_signal(signal.SIGUSR1)
                # The signal handler runs before this HTTP request is handled by the main process.
                import time
                time.sleep(.05)
                self.assertEqual(request(port)[0], 200)
        finally:
            proc.terminate()
            out, err = proc.communicate(timeout=5)
        self.assertEqual(out, '')
        self.assertNotIn('private-session', err)
        self.assertNotIn('private-body', err)
        self.assertEqual(proc.returncode, 0)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))

    def test_mcp_stdio_tools_http_errors_and_exit_cleanup(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise():
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT/'mcp_server.py')])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    self.assertEqual({t.name for t in tools.tools}, {
                        'bridge_devices', 'bridge_start', 'bridge_status',
                        'bridge_set_fault', 'bridge_requests', 'bridge_stop'})
                    async def call(name, args=None):
                        result = await client.call_tool(name, args or {})
                        self.assertFalse(result.isError, result.content)
                        return json.loads(result.content[0].text)
                    self.assertFalse((await call('bridge_status'))['running'])
                    state = await call('bridge_start', {'host': 'api.example.test', 'proxy_port': free_port(),
                                       'backend_port': self.backend.server_port, 'reject_path': '/upload'})
                    self.assertEqual(await asyncio.to_thread(request, state['proxy_port']), (503, b''))
                    invalid = await client.call_tool('bridge_stop', {'session_id': 'stale'})
                    self.assertTrue(invalid.isError)
                    await call('bridge_set_fault', {'session_id': state['session_id']})
                    self.assertEqual((await asyncio.to_thread(request, state['proxy_port']))[0], 200)
                    events = await call('bridge_requests')
                    self.assertEqual(len(events['requests']), 2)
                    self.assertNotIn('private-session', json.dumps(events))
                    await call('bridge_stop', {'session_id': state['session_id']})
                    state = await call('bridge_start', {'host': 'api.example.test', 'proxy_port': state['proxy_port'],
                                       'backend_port': self.backend.server_port})
                    return state['proxy_port']
        port = asyncio.run(exercise())
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))


if __name__ == '__main__':
    unittest.main()
