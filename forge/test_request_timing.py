"""Offline timeout policy and stream completion boundaries."""
import io
import json
import urllib.error
import urllib.request
import threading
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from .model import Provider, HttpTransport, ModelRouter, BadRequest, ModelCancelled, RateLimited
from .request_timing import request_timeout, gateway_client_timeout
from .secret_http import iter_model_sse, SecretSSEFilter
from .gateway import GatewayConfig, serve
from .cache_state import CacheWarmth


def frame(event):
    return b'data: ' + json.dumps(event).encode() + b'\n\n'


class TimedOutStream(io.BytesIO):
    def __init__(self, content, error=None):
        super().__init__(content)
        self.error = error or TimeoutError('timed out')
        self.eof_reads = 0

    def readline(self, *args):
        line = super().readline(*args)
        if not line:
            self.eof_reads += 1
            raise self.error
        return line


class ModelTimeoutTests(unittest.TestCase):
    def setUp(self):
        stub = patch('forge.model.warmth_store', return_value=CacheWarmth())
        stub.start()
        self.addCleanup(stub.stop)

    def test_default_and_explicit_timeouts_reach_socket_without_rounding(self):
        seen = []
        def opener(request, **kwargs):
            seen.append((kwargs['timeout'], request.data))
            return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
        transport = HttpTransport(response_opener=opener)
        for limit in (0, .75, 900):
            transport.complete(Provider('p', 'https://example.invalid', request_timeout=limit),
                               'stub', [{'role': 'user', 'content': 'same prompt'}])
        self.assertEqual([v[0] for v in seen], [600, .75, 900])
        self.assertEqual(len({v[1] for v in seen}), 1, 'timing must not affect request/cache bytes')
        self.assertEqual(HttpTransport(timeout=7).timeout, 7)
        self.assertEqual(gateway_client_timeout(900), 960)

    def test_invalid_timeouts_fail_before_network(self):
        for value in (-1, float('inf'), float('nan'), True, 'invalid'):
            with self.subTest(value=value), self.assertRaises(BadRequest):
                Provider('p', 'https://example.invalid', request_timeout=value)
        self.assertEqual(request_timeout('12.5'), 12.5)

    def test_gateway_retains_configured_timeout(self):
        cfg = GatewayConfig(upstream='https://example.invalid', api_key='', models=['stub'],
                            provider_options={'requestTimeout': 900})
        _, _, provider = cfg.prepare_request({'model': 'stub', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(provider.request_timeout, 900)
        cfg.provider_options['requestTimeout'] = -2
        with self.assertRaises(ValueError):
            cfg.prepare_request({'model': 'stub', 'messages': []})

    def test_upstream_request_timeout_is_retryable_but_bad_auth_is_not(self):
        for status, expected_calls in ((408, 2), (524, 2), (401, 1)):
            with self.subTest(status=status), patch('forge.model.time.sleep'):
                calls = []
                def opener(request, **kwargs):
                    calls.append(request.data)
                    if len(calls) == 1:
                        raise urllib.error.HTTPError('https://example.invalid', status, 'upstream', {}, io.BytesIO(b'{}'))
                    return io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')
                provider = Provider('p', 'https://example.invalid', default_model='stub')
                router = ModelRouter([provider], transport=HttpTransport(response_opener=opener), retries_per_provider=1)
                if status == 401:
                    with self.assertRaises(BadRequest):
                        router.complete([{'role': 'user', 'content': 'hello'}])
                else:
                    self.assertEqual(router.complete([{'role': 'user', 'content': 'hello'}]).text, 'ok')
                    self.assertEqual(calls[0], calls[1])
                self.assertEqual(len(calls), expected_calls)

    def test_cancel_interrupts_backoff_without_second_request(self):
        event = threading.Event()
        class Transport:
            calls = 0
            def complete(self, *args, **kwargs):
                self.calls += 1
                event.set()
                raise RateLimited('slow down', retry_after=30)
        transport = Transport()
        router = ModelRouter([Provider('p', 'https://example.invalid', default_model='stub')], transport=transport)
        with self.assertRaises(ModelCancelled):
            router.complete([], cancel_event=event)
        self.assertEqual(transport.calls, 1)

    def test_premium_cancellation_is_not_downgraded_to_successful_draft(self):
        from .routing import SmartRouter, RoutingConfig
        from .model import Usage
        class Transport:
            def complete(self, provider, model, messages, **options):
                if model == 'draft':
                    return 'draft answer', Usage()
                raise ModelCancelled('已停止模型请求')
        router = SmartRouter([Provider('p', 'https://example.invalid')], transport=Transport(),
            routing=RoutingConfig(strategy='premium', tiers=[('p', 'draft')], premium=[('p', 'judge')]))
        with self.assertRaises(ModelCancelled):
            router.complete([{'role': 'user', 'content': 'write'}])

    def test_backend_agent_cancel_interrupts_header_wait_and_never_retries(self):
        from .loop import Agent
        from .policy import Policy, Mode
        from .tools import build_builtin_registry
        started, release, cancel = threading.Event(), threading.Event(), threading.Event()
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                started.set()
                release.wait(3)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                provider = Provider('p', f'http://127.0.0.1:{server.server_port}', default_model='stub')
                agent = Agent(home=root, workspace=root, router=ModelRouter([provider]),
                    registry=build_builtin_registry(), policy=Policy(mode=Mode.PLAN, workspace=root),
                    cancellation_event=cancel, project_rules=False)
                result = []
                worker = threading.Thread(target=lambda: result.append(agent.run('wait then cancel')), daemon=True)
                worker.start()
                try:
                    self.assertTrue(started.wait(2))
                    cancel.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(result[0].stopped, 'cancelled')
                    self.assertEqual(len(calls), 1)
                finally:
                    cancel.set()
                    release.set()
                    worker.join(3)
                    agent.secret_scope.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(2)


class StreamTimeoutTests(unittest.TestCase):
    def test_terminal_delta_preserves_text_order_and_usage_with_buffered_suffix(self):
        from .secrets import SecretScope
        scope = SecretScope()
        scope.reference('d-offline-synthetic-secret')
        try:
            fence = SecretSSEFilter()
            data = frame({'choices': [{'index': 0, 'delta': {'content': 'finished'}, 'finish_reason': 'stop'}],
                          'usage': {'prompt_tokens': 20}})
            events = [json.loads(line[5:]) for line in fence.feed(data).splitlines() if line.startswith(b'data:')]
            content = ''.join(c.get('delta', {}).get('content', '') for e in events for c in e.get('choices', []))
            self.assertEqual(content, 'finished')
            self.assertEqual(sum('usage' in e for e in events), 1)
            self.assertEqual(events[-1]['choices'][0]['finish_reason'], 'stop')
        finally:
            scope.close()

    def test_finished_stream_keeps_trailing_usage_and_tolerates_close_timeout(self):
        content = frame({'choices': [{'index': 0, 'delta': {'content': 'done'}, 'finish_reason': 'stop'}]})
        content += frame({'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 4}})
        response = TimedOutStream(content)
        self.assertEqual(b''.join(iter_model_sse(response)), content)
        self.assertEqual(response.eof_reads, 1)

    def test_finished_choice_does_not_flush_another_choices_secret_prefix(self):
        from .secrets import SecretScope
        scope = SecretScope()
        secret = 'z-offline-choice-secret-value'
        scope.reference(secret)
        try:
            fence = SecretSSEFilter()
            first = frame({'choices': [
                {'index': 0, 'delta': {'content': 'first'}, 'finish_reason': 'stop'},
                {'index': 1, 'delta': {'content': secret[:8]}, 'finish_reason': None}]})
            second = frame({'choices': [{'index': 1, 'delta': {'content': secret[8:]}, 'finish_reason': 'stop'}]})
            output = fence.feed(first) + fence.feed(second)
            events = [json.loads(line[5:]) for line in output.splitlines() if line.startswith(b'data:')]
            content = ''.join(c.get('delta', {}).get('content', '') for e in events
                              for c in e.get('choices', []) if c.get('index') == 1)
            self.assertEqual(content, '[REDACTED_SECRET]')
        finally:
            scope.close()

    def test_partial_text_reasoning_and_tools_still_fail(self):
        for delta in ({'content': 'partial'}, {'reasoning_content': 'thinking'},
                      {'tool_calls': [{'index': 0, 'function': {'arguments': '{'}}]}):
            response = TimedOutStream(frame({'choices': [{'delta': delta, 'finish_reason': None}]}))
            with self.assertRaises(TimeoutError):
                list(iter_model_sse(response))

    def test_all_choices_must_finish_before_tolerating_timeout(self):
        first = frame({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
        with self.assertRaises(TimeoutError):
            list(iter_model_sse(TimedOutStream(first), expected_choices=2))
        second = frame({'choices': [{'index': 1, 'delta': {}, 'finish_reason': 'stop'}]})
        self.assertEqual(b''.join(iter_model_sse(TimedOutStream(first + second), expected_choices=2)), first + second)

    def test_protocol_end_never_waits_for_socket_eof(self):
        for end in (b'data: [DONE]\n\n', frame({'type': 'message_stop'})):
            response = TimedOutStream(end)
            self.assertTrue(list(iter_model_sse(response)))
            self.assertEqual(response.eof_reads, 0)

    def test_network_failures_other_than_timeout_are_not_hidden(self):
        complete = frame({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        with self.assertRaises(ConnectionResetError):
            list(iter_model_sse(TimedOutStream(complete, ConnectionResetError('reset'))))
        error = urllib.error.URLError(TimeoutError('timed out'))
        self.assertEqual(b''.join(iter_model_sse(TimedOutStream(complete, error))), complete)

    def test_gateway_stream_and_translation_keep_completed_reply_on_timeout(self):
        complete = frame({'choices': [{'index': 0, 'delta': {'content': 'finished'}, 'finish_reason': 'stop'}]})
        complete += frame({'choices': [], 'usage': {'prompt_tokens': 20, 'completion_tokens': 2}})
        for path in ('/v1/chat/completions', '/v1/messages'):
            with self.subTest(path=path):
                cfg = GatewayConfig(upstream='https://example.invalid/v1', api_key='', port=0,
                    models=['stub'], upstream_wire='openai', provider_options={'requestTimeout': 900})
                cfg.record_usage = lambda *args: recorded.append(args)
                recorded = []
                response = TimedOutStream(complete)
                response.headers, response.status = {'content-type': 'text/event-stream'}, 200
                server = serve(cfg)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with patch('forge.gateway.open_upstream', return_value=response) as opened:
                        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                            data=json.dumps({'model': 'stub', 'stream': True, 'max_tokens': 20,
                                             'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
                            headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(request, timeout=3) as result:
                            body = result.read()
                        self.assertEqual(opened.call_args.kwargs['timeout'], 900)
                    events = [json.loads(line[5:]) for line in body.splitlines() if line.startswith(b'data:')]
                    content = ''.join(c.get('delta', {}).get('content', '') for e in events for c in e.get('choices', []))
                    if path.endswith('/messages'):
                        content = ''.join(e.get('delta', {}).get('text', '') for e in events)
                    self.assertEqual(content, 'finished')
                    self.assertNotIn(b'api_error', body)
                    self.assertEqual(recorded[0][2]['prompt_tokens'], 20)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)


if __name__ == '__main__':
    unittest.main()
