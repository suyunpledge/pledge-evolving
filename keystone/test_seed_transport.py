"""Exercise real HTTP framing on loopback with the TLS connection factory replaced."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from .seed_transport import CHAT_URL, CONNECT_TIMEOUT, SeedTransport


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
