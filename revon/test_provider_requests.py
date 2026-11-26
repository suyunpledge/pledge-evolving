import io
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from .config import Config
from .development.models import ModelSettingsSource
from .gateway import GatewayConfig, serve
from .provider_requests import open_recorded, recent_requests, safe_url


class ProviderRequestTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_safe_path_and_concurrent_attempts(self):
        url = 'https://user:private@example.org:8443/custom/key-in-path/v1/chat/completions?token=private#secret'
        self.assertEqual(safe_url(url), 'https://example.org:8443/…/…/v1/chat/completions')
        self.assertEqual(safe_url('http://[::1]:11434/v1'), 'http://[::1]:11434/v1')
        for bad in ('invalid', 'file:///secret', 'https://h:invalid/v1', 'https://test.invalid/\n'):
            self.assertEqual(safe_url(bad), '')
        self.assertEqual(recent_requests(self.home), [])
        self.assertFalse(list(self.home.iterdir()))
        def attempt(_):
            def opener(req):
                response = io.BytesIO()
                response.status = 200
                return response
            with open_recorded(opener, urllib.request.Request(url), home=self.home, provider_id='row-a',
                               base_url=url, source='direct'):
                pass
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(attempt, range(12)))
        rows = recent_requests(self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['attempts'], rows[0]['http_status']), (12, 200))
        raw = next(self.home.glob('*.sqlite3')).read_bytes()
        for value in (b'private', b'key-in-path', b'?token=', b'custom'):
            self.assertNotIn(value, raw)

    def test_no_http_response_and_unwritable_store_do_not_change_transport_behavior(self):
        base = 'https://example.org/v1'
        req = urllib.request.Request(base + '/messages')
        def cannot_connect(_request):
            raise ConnectionRefusedError('fixture')
        options = dict(provider_id='example', source='direct', base_url=base)
        with self.assertRaises(ConnectionRefusedError), open_recorded(cannot_connect, req, home=self.home, **options):
            self.fail('connection error was swallowed')
        self.assertIsNone(recent_requests(self.home)[0]['http_status'])
        with open_recorded(lambda _: io.BytesIO(b'ok'), req, home=self.home, provider_id=None,
                           source='gateway', base_url=base) as response:
            response.read()
        self.assertEqual(recent_requests(self.home)[0]['provider_id'], '')
        blocked = self.home / 'not-a-directory'
        blocked.touch()
        with open_recorded(lambda _: io.BytesIO(b'ok'), req, home=blocked, **options) as result:
            self.assertEqual(result.read(), b'ok')

    def test_coding_and_gateway_record_wire_paths_without_counting_model_listing(self):
        class Upstream(BaseHTTPRequestHandler):
            status = 200
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(self.status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"private-test-content"},"finish_reason":"stop"}]}')
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.serve(upstream)
        base = f'http://127.0.0.1:{upstream.server_port}/v1'
        rows = [{'id': 'my-provider', 'name': 'provider:my-provider', 'config': {'baseURL': base, 'model': 'alias'}}]
        (self.home / 'revon.patch.json').write_text(json.dumps(rows), encoding='utf8')
        router = ModelSettingsSource(self.home)(Config(), self.home / 'project-control')
        router.complete([{'role': 'user', 'content': 'private-test-prompt'}], primary=('my-provider', 'alias'))
        cfg = GatewayConfig(upstream=base, port=0, models=['alias'], wire='openai', upstream_wire='openai',
                            home=self.home, provider_options={'providerId': 'my-provider'})
        front = serve(cfg)
        self.serve(front)
        endpoint = f'http://127.0.0.1:{front.server_port}/v1'
        with urllib.request.urlopen(endpoint + '/models', timeout=4) as res:
            self.assertEqual(res.status, 200)
        self.assertEqual([r['source'] for r in recent_requests(self.home)], ['direct'])
        request = urllib.request.Request(endpoint + '/chat/completions',
            data=b'{"model":"alias","messages":[{"role":"user","content":"private-test-prompt"}]}',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=4) as res:
            self.assertEqual(res.status, 200)
        Upstream.status = 429
        with self.assertRaises(urllib.error.HTTPError) as refusal:
            urllib.request.urlopen(request, timeout=4)
        refusal.exception.close()
        gateway, direct = recent_requests(self.home)
        for row in (gateway, direct):
            self.assertEqual(row['provider_id'], 'my-provider')
            self.assertEqual(row['base_url'], base)
            self.assertEqual(row['request_url'], base + '/chat/completions')
        self.assertEqual((gateway['source'], gateway['http_status'], gateway['attempts']), ('gateway', 429, 2))
        self.assertEqual((direct['source'], direct['http_status'], direct['attempts']), ('direct', 200, 1))
        # A legacy launcher cannot identify a settings row, but does know the upstream it contacted.
        cfg.provider_options.clear()
        Upstream.status = 200
        with urllib.request.urlopen(request, timeout=4) as res:
            self.assertEqual(res.status, 200)
        anonymous = recent_requests(self.home)[0]
        self.assertEqual((anonymous['provider_id'], anonymous['base_url'], anonymous['attempts']), ('', base, 1))
        raw = next(self.home.glob('*.sqlite3')).read_bytes()
        self.assertNotIn(b'private-test-prompt', raw)
        self.assertNotIn(b'private-test-content', raw)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
