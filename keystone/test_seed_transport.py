"""Exercise real HTTP framing on loopback with the TLS connection factory replaced."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from .seed_transport import CHAT_URL, CONNECT_TIMEOUT, SeedConnectFailed, SeedHeaderInterrupted, SeedTransport


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except ConnectionResetError:  # Tests deliberately abandon incomplete responses.
            self.close_connection = True

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.bodies.append(body)
        status = int(body)
        self.send_response(status)
        self.send_header('Transfer-Encoding', 'chunked')
        if status == 302:
            self.send_header('Location', 'https://example.invalid/')
        self.end_headers()
        data = b'data: [DONE]\n\n' if status == 200 else b'rejected'
        self.wfile.write(b'%x\r\n' % len(data) + data + b'\r\n0\r\n\r\n')


class SeedTransportTests(unittest.TestCase):
    def setUp(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.bodies = []
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.pool = SeedTransport()
        self.addCleanup(self.pool.close)
        self.connections = []

        def local(host, *, timeout):
            self.assertEqual(host, 'ark.cn-beijing.volces.com')
            self.connections.append(timeout)
            return http.client.HTTPConnection(*server.server_address, timeout=timeout)
        patcher = patch('keystone.seed_transport.HTTPSConnection', side_effect=local)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, status=200, timeout=90, timings=None):
        return self.pool.open(Request(CHAT_URL, data=str(status).encode()), timeout=timeout, timings=timings)

    def test_short_connect_long_read_and_reuse_after_sse_marker(self):
        for timeout in (90, 45):
            timings = {}
            with self.open(timeout=timeout, timings=timings) as response:
                self.assertEqual(response.fp.raw._sock.gettimeout(), timeout)
                self.assertEqual(response.readline(), b'data: [DONE]\n')
            if timeout == 90:
                self.assertRegex(timings['seed_connection'], r'^seed_connect;dur=\d+$')
            else:
                self.assertEqual(timings['seed_connection'], 'seed_reuse;dur=0')
        self.assertEqual(self.connections, [CONNECT_TIMEOUT])
        self.assertEqual(self.server.bodies, [b'200'] * 2)

    def test_handshake_failure_only_shortens_the_retryable_first_connect(self):
        from .seed_transport import FIRST_CONNECT_TIMEOUT
        request = Request(CHAT_URL, data=b'200')
        with patch.object(http.client.HTTPConnection, 'connect', side_effect=TimeoutError('handshake')):
            with self.assertRaises(SeedConnectFailed), self.pool.open(
                    request, timeout=90, probe_reuse=True):
                self.fail('the HTTP request must not start')
        self.assertEqual(self.server.bodies, [])
        with self.pool.open(request, timeout=90, probe_reuse=False) as response:
            self.assertEqual(response.fp.raw._sock.gettimeout(), 90)
            response.read()
        self.assertEqual(self.connections, [FIRST_CONNECT_TIMEOUT, CONNECT_TIMEOUT])
        self.assertEqual(self.server.bodies, [b'200'])

    def test_incomplete_body_errors_and_redirects_retire_socket(self):
        with self.open(timeout=2) as response:
            self.assertEqual(response.read(1), b'd')
        with self.assertRaises(HTTPError) as exc:
            with self.open(502):
                self.fail('HTTP error should propagate')
        self.assertEqual(exc.exception.read(), b'rejected')
        with self.assertRaises(PermissionError):
            with self.open(302):
                self.fail('must not follow redirects')
        self.assertEqual(self.connections, [2, CONNECT_TIMEOUT, CONNECT_TIMEOUT])
        self.assertEqual(self.server.bodies, [b'200', b'502', b'302'])

    def test_silent_reused_socket_expires_and_retry_uses_fresh_connection(self):
        request = Request(CHAT_URL, data=b'200')
        timings = {}
        release = threading.Event()
        def stall(handler):
            self.server.bodies.append(handler.rfile.read(int(handler.headers['Content-Length'])))
            release.wait(4)
            handler.close_connection = True

        with self.open() as response:
            response.read()
        try:
            with (patch.object(Handler, 'do_POST', stall),
                  patch('keystone.seed_transport.REUSED_HEADER_TIMEOUT', .15),
                  self.assertRaises(SeedHeaderInterrupted)):
                with self.pool.open_coding(request, timeout=3, timings=timings, probe_reuse=True):
                    self.fail('silent socket returned a response')
        finally:
            release.set()
        self.assertEqual(timings['seed_connection'], 'seed_reuse;dur=0')
        self.assertEqual(timings['header_timeout_s'], .15)
        self.assertEqual(timings['header_failure'], 'TimeoutError')
        self.assertIsNone(self.pool.idle)
        self.assertEqual(len(self.connections), 1)  # transport did not retry the POST
        timings = {}
        with self.pool.open_coding(request, timeout=3, timings=timings) as response:
            response.read()
        self.assertEqual(timings['header_timeout_s'], 3)
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(self.server.bodies, [b'200'] * 3)

    def test_probe_preserves_body_timeout_and_shorter_provider_settings(self):
        with patch('keystone.seed_transport.REUSED_HEADER_TIMEOUT', .5):
            for timeout, expected in [(5, 5), (6, .5), (.4, .4)]:
                timings = {}
                with self.pool.open_coding(Request(CHAT_URL, data=b'200'), timeout=timeout,
                                           timings=timings, probe_reuse=True) as response:
                    self.assertEqual(timings['header_timeout_s'], expected)
                    self.assertEqual(response.fp.raw._sock.gettimeout(), timeout)
                    response.read()

    def test_response_consumer_failure_does_not_replay_and_next_request_uses_fresh_socket(self):
        with self.assertRaises(ValueError):
            with self.open():
                raise ValueError('incomplete stream')
        with self.open() as response:
            response.read()
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(self.server.bodies, [b'200', b'200'])
        with self.assertRaises(ValueError):
            with self.pool.open(Request('https://elsewhere.invalid/', data=b'200'), timeout=45):
                self.fail('unrelated origin')

    def test_cancelling_reused_socket_wakes_header_wait_and_retires_connection(self):
        from .http_transport import RequestCancelled
        with self.open() as response:
            response.read()
        received, release, cancel = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def stalled(handler):
            received.set()
            release.wait(4)
            handler.close_connection = True
        errors = []
        def request():
            try:
                with self.pool.open(Request(CHAT_URL, data=b'200'), timeout=45, cancel_event=cancel, probe_reuse=True):
                    pass
            except Exception as exc:
                errors.append(exc)
        with patch.object(Handler, 'do_POST', stalled):
            worker = threading.Thread(target=request)
            worker.start()
            self.assertTrue(received.wait(2))
            cancel.set()
            worker.join(2)
            release.set()
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], RequestCancelled)
        self.assertIsNone(self.pool.idle)

    def test_outer_step_owns_exactly_one_transport_attempt(self):
        from .model import ModelRouter, Provider, RateLimited
        from unittest.mock import Mock
        transport = Mock()
        limited = RateLimited('busy', retry_after=7)
        transport.complete.side_effect = limited
        model = 'doubao-seed-evolving'
        provider = Provider('seed', CHAT_URL.removesuffix('/chat/completions'), default_model=model)
        router = ModelRouter([provider], primary=('seed', model), transport=transport, retries_per_provider=2)
        with self.assertRaises(RateLimited) as error:
            router.complete([], step_owns_seed_retry=True)
        self.assertIs(error.exception, limited)
        self.assertEqual(transport.complete.call_count, 1)

    def test_proxy_recovery_only_before_post_and_after_two_failed_connects(self):
        from contextlib import nullcontext
        request = Request(CHAT_URL, data=b'200')
        timings = {}
        with (patch.object(self.pool, 'open', side_effect=SeedConnectFailed('connect')),
              patch('keystone.seed_transport.getproxies', return_value={'https': 'http://proxy.invalid'}),
              patch('keystone.seed_transport.proxy_bypass', return_value=False),
              patch('keystone.http_transport.open_response', return_value=nullcontext('response')) as fallback):
            with self.assertRaises(SeedConnectFailed), self.pool.open_coding(request, timings=timings, timeout=5):
                self.fail()
            fallback.assert_not_called()
            with self.pool.open_coding(request, timings=timings, timeout=5) as response:
                self.assertEqual(response, 'response')
            self.assertEqual(timings['seed_connection'], 'system_proxy_after_connect')
        with (patch.object(self.pool, 'open', side_effect=ConnectionResetError('after POST')),
              patch('keystone.http_transport.open_response') as fallback):
            with self.assertRaises(ConnectionResetError), self.pool.open_coding(request, timings={}, timeout=5):
                self.fail()
            fallback.assert_not_called()
