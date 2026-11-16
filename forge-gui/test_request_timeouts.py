"""Model waits, completed SSE and tool timeouts use different boundaries."""
import io
import json
import threading
import time
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forge_client import ForgeGatewayClient, ChatMessage, GatewayError
from forge.test_request_timing import TimedOutStream, frame
from forge.request_timing import MODEL_RESPONSE_TIMEOUT
from forge.secrets import SecretScope
from config_model import normalize
from phase_client import PhaseClient
import sub_agent


class ClientTimeoutTests(unittest.TestCase):
    def test_model_defaults_align_and_explicit_limits_survive(self):
        client = ForgeGatewayClient()
        self.assertEqual(client.timeout, 660)
        client.set_upstream_timeout(900)
        self.assertEqual(client.timeout, 960)
        explicit = ForgeGatewayClient(timeout=.25)
        explicit.set_upstream_timeout(900)
        self.assertEqual(explicit.timeout, .25)
        self.assertEqual(sub_agent.SubAgent('id', 'role', 'system', 'user').timeout_s, MODEL_RESPONSE_TIMEOUT)
        rows = [{'id': 'p', 'config': {'baseURL': 'https://example.invalid', 'model': 'stub'}}]
        scope = SecretScope()
        try:
            phase = PhaseClient(rows, ['p', 'stub'], {}, scope)
            self.assertEqual(phase.router.transport.timeout, MODEL_RESPONSE_TIMEOUT)
        finally:
            scope.close()

    def test_flat_provider_import_keeps_fractional_timeout(self):
        config = normalize(json.dumps({'id': 'p', 'baseURL': 'https://example.invalid',
                                      'model': 'stub', 'requestTimeout': 900.5}), add_model=False).rows[0]['config']
        self.assertEqual(config['requestTimeout'], 900.5)

    def test_desktop_launch_passes_timeout_to_gateway(self):
        from unittest.mock import Mock
        import forge_gui_v2 as gui
        from test_layout_dpi import isolated_app, pump
        from test_gateway_startup_lifecycle import configure
        with isolated_app() as (root, app, errors):
            configure(app)
            app.user_rows[0]['config']['requestTimeout'] = 900
            with patch.object(gui, 'port_in_use', return_value=False), \
                 patch.object(gui, 'bindable_gateway_port', return_value=18899), \
                 patch.object(gui.subprocess, 'Popen', return_value=Mock()) as spawn, \
                 patch.object(app, '_gateway_started'):
                app._start_gateway()
                pump(root, .15)
                command = spawn.call_args.args[0]
                options = json.loads(command[command.index('--provider-options') + 1])
                self.assertEqual(options['requestTimeout'], 900)
            self.assertEqual(errors, [])

    def test_completed_stream_is_not_lost_during_close_timeout(self):
        wire = frame({'choices': [{'delta': {'content': 'complete'}, 'finish_reason': 'stop'}]})
        wire += frame({'choices': [], 'usage': {'prompt_tokens': 30}})
        seen = []
        with patch('forge_client.open_response', return_value=TimedOutStream(wire)) as opened:
            result = ForgeGatewayClient().stream_chat([], on_chunk=seen.append)
        self.assertEqual(result.text, 'complete')
        self.assertEqual(''.join(seen), 'complete')
        self.assertEqual(result.usage, {'prompt_tokens': 30})
        self.assertEqual(opened.call_count, 1)

    def test_partial_tool_stream_stays_failed_and_is_never_replayed(self):
        wire = frame({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a',
            'function': {'name': 'write_file', 'arguments': '{'}}]}, 'finish_reason': None}]})
        with patch('forge_client.open_response', return_value=TimedOutStream(wire)) as opened:
            with self.assertRaises(GatewayError):
                ForgeGatewayClient().stream_chat([])
        self.assertEqual(opened.call_count, 1)

    def test_output_limit_is_not_mistaken_for_a_successful_tool_completion(self):
        wire = frame({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a',
            'function': {'name': 'write_file', 'arguments': '{}'}}]}, 'finish_reason': 'length'}]})
        with patch('forge_client.open_response', return_value=TimedOutStream(wire)) as opened:
            with self.assertRaisesRegex(GatewayError, '输出达到上限'):
                ForgeGatewayClient().stream_chat([])
        self.assertEqual(opened.call_count, 1)

    def test_tool_timeout_keeps_action_identity_without_automatic_resend(self):
        client = ForgeGatewayClient()
        with patch('forge_client.open_response', side_effect=TimeoutError('timed out')) as opened:
            with self.assertRaises(GatewayError) as caught:
                client.call_tool('write_file', {'path': 'example.txt', 'content': 'content'})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.kwargs['timeout'], 90)
        self.assertTrue(caught.exception.run_id)
        self.assertTrue(caught.exception.action_id)

    def test_metadata_probe_does_not_inherit_long_generation_timeout(self):
        with patch('forge_client.open_response', return_value=io.BytesIO(b'{"data":[]}')) as opened:
            ForgeGatewayClient().list_tools()
        self.assertEqual(opened.call_args.kwargs['timeout'], 10)

    def test_shell_execution_budget_has_transport_margin(self):
        client = ForgeGatewayClient()
        for explicit, expected in ((None, 150), (2, 2)):
            with patch('forge_client.open_response', return_value=io.BytesIO(b'{"ok":true}')) as opened:
                client.call_tool('shell_exec', {'command': 'echo test', 'timeout': 120}, timeout=explicit)
            self.assertEqual(opened.call_args.kwargs['timeout'], expected)

    def test_real_socket_completed_response_survives_delayed_close(self):
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(frame({'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]}))
                self.wfile.flush()
                release.wait(2)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ForgeGatewayClient(f'http://127.0.0.1:{server.server_port}', timeout=5)
            before = time.monotonic()
            with patch('forge.request_timing.STREAM_FINISH_GRACE', .05):
                result = client.stream_chat([ChatMessage('user', 'hello')], cancel_event=threading.Event())
            self.assertEqual(result.text, 'done')
            self.assertLess(time.monotonic() - before, 1, 'finished stream must not wait for generation timeout')
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
