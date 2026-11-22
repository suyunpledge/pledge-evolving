"""Stream recovery uses the same model history and never dispatches a partial tool call."""
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from keystone.request_timing import transient_disconnect
from keystone_client import ChatMessage, GatewayError, GenerationCancelled, KeystoneGatewayClient, ModelStreamInterrupted
from .conversation import run
from .productivity_tools import with_productivity


def frame(delta, finish=None):
    event = {'choices': [{'delta': delta, 'finish_reason': finish}]}
    return io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())


def document(identifier, reverse=False):
    args = {'title': '周报', 'sections': [{'paragraphs': ['正文']}]}
    if reverse:
        args = dict(reversed(args.items()))
    return {'id': identifier, 'type': 'function', 'index': 0,
            'function': {'name': 'keystone_create_document', 'arguments': json.dumps(args)}}


class StreamRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.messages = [ChatMessage('user', '生成周报')]
        self.status = []
        self.cancel = threading.Event()
        self.client = KeystoneGatewayClient()
        self.tools = with_productivity(({}, None), self.folder)
        self.text = []
        self.checkpoints = []
        self.options = dict(model='test', effort=None, cancel=self.cancel, authorize=lambda *_: None,
                            chunk=self.text.append, next_round=self.text.clear, status=self.status.append,
                            attachment_folder=self.folder, checkpoint=self.checkpoints.append,
                            chat_mcp=self.tools, mcp=self.tools)
        throttle = patch('qt_frontend.conversation.MODEL_RETRY_DELAYS', (0, 0))
        throttle.start()
        self.addCleanup(throttle.stop)

    def drive(self):
        return run(self.client, self.messages, **self.options)

    def test_partial_tool_arguments_then_502_after_file_publication(self):
        partial = document('discarded')
        partial['function']['arguments'] = '{"title":'
        reply = [frame({'content': '中断的回复', 'tool_calls': [partial]}),
                 frame({'tool_calls': [document('original')]}, 'tool_calls'),
                 HTTPError('http://localhost', 502, 'bad gateway', {}, io.BytesIO(b'{}')),
                 frame({'tool_calls': [document('new-id', reverse=True)]}, 'tool_calls'),
                 frame({'content': '已生成'}, 'stop')]
        with patch('keystone_client.open_response', side_effect=reply) as opened:
            result = self.drive()
        bodies = [json.loads(call.args[0].data) for call in opened.call_args_list]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[2], bodies[3])
        self.assertEqual(result.text, ''.join(self.text))
        self.assertEqual(result.text, '已生成')
        self.assertEqual(len([s for s in self.status if '自动重试' in s]), 2)
        self.assertEqual(len(self.checkpoints), 2)
        receipts = [json.loads(m.content) for m in self.messages if m.role == 'tool']
        self.assertEqual(receipts[0]['meta']['attachment'], receipts[1]['meta']['attachment'])
        self.assertTrue(receipts[1]['meta']['reused_attachment'])
        self.assertEqual(len(list(self.folder.rglob('*.docx'))), 1)

        # A manual retry from the saved checkpoint can use the same receipt too.
        self.messages = [ChatMessage.from_session_dict(m.to_session_dict()) for m in self.checkpoints[0]]
        def repeat():
            with patch('keystone_client.open_response', side_effect=[
                    frame({'tool_calls': [document('another-id')]}, 'tool_calls'),
                    frame({'content': '好了'}, 'stop')]):
                self.drive()
        repeat()
        self.assertEqual(len(list(self.folder.rglob('*.docx'))), 1)
        self.messages.append(ChatMessage('user', '再生成一份完全相同的周报'))
        repeat()
        self.assertEqual(len(list(self.folder.rglob('*.docx'))), 2)

    def test_exhaustion_cancel_and_fatal_errors(self):
        with patch.object(self.client, 'stream_chat', side_effect=ModelStreamInterrupted('EOF')) as model:
            with self.assertRaises(ModelStreamInterrupted):
                self.drive()
            self.assertEqual(model.call_count, 3)

        self.options['status'] = lambda _: self.cancel.set()
        with patch.object(self.client, 'stream_chat', side_effect=ModelStreamInterrupted('EOF')) as model:
            with self.assertRaises(GenerationCancelled):
                self.drive()
            model.assert_called_once()

        self.cancel.clear()
        for code, headers in [(400, {}), (401, {}), (502, {'X-Keystone-Retryable': '0'})]:
            with self.subTest(code=code), patch('keystone_client.open_response', side_effect=HTTPError(
                    'http://localhost', code, 'failure', headers, io.BytesIO(b'{}'))) as opened:
                with self.assertRaises(GatewayError) as err:
                    self.drive()
                self.assertNotIsInstance(err.exception, ModelStreamInterrupted)
                opened.assert_called_once()

    def test_retries_across_many_tool_steps_have_a_total_limit(self):
        from .conversation import MAX_GENERATION_RETRIES
        responses = []
        for index in range(MAX_GENERATION_RETRIES):
            tool = {'id': f'math-{index}', 'index': 0, 'type': 'function', 'function': {
                'name': 'keystone_evaluate_math', 'arguments': json.dumps({'expression': str(index)})}}
            responses.extend([ConnectionResetError(), frame({'tool_calls': [tool]}, 'tool_calls')])
        responses.append(ConnectionResetError())
        with patch('keystone_client.open_response', side_effect=responses) as opened:
            with self.assertRaises(ModelStreamInterrupted):
                self.drive()
        self.assertEqual(opened.call_count, len(responses))
        self.assertEqual(len(self.checkpoints), MAX_GENERATION_RETRIES)

    def test_gateway_failure_during_stream_and_completed_connection_reset(self):
        self.assertTrue(transient_disconnect(URLError(ssl.SSLEOFError(8, 'unexpected EOF'))))
        self.assertFalse(transient_disconnect(URLError(ssl.SSLCertVerificationError(1, 'cert'))))
        event = {'error': {'type': 'api_error', 'code': 'keystone_upstream_failure', 'retryable': True}}
        error_stream = io.BytesIO(('data: ' + json.dumps(event) + '\n\n').encode())

        class ResetAfterFinish(io.BytesIO):
            def readline(self, limit):
                line = super().readline(limit)
                if not line:
                    raise IncompleteRead(b'')
                return line

        complete = ResetAfterFinish(frame({'content': '答复'}, 'stop').getvalue())
        with patch('keystone_client.open_response', side_effect=[error_stream, complete]) as opened:
            self.assertEqual(self.drive().text, '答复')
        self.assertEqual(opened.call_count, 2)

    def test_gateway_marks_transport_eof_before_and_after_headers(self):
        from keystone.gateway import GatewayConfig, serve

        class CutStream(io.BytesIO):
            headers = {'content-type': 'text/event-stream'}
            status = 200

            def readline(self, limit):
                line = super().readline(limit)
                if not line:
                    raise IncompleteRead(b'')
                return line

        server = serve(GatewayConfig(upstream='https://example.invalid/v1', api_key='', port=0,
                                     models=['test'], upstream_wire='openai'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = KeystoneGatewayClient(f'http://127.0.0.1:{server.server_port}', timeout=3)
        try:
            upstream = [URLError(ssl.SSLEOFError(8, 'EOF')), CutStream(frame({'content': 'partial'}).getvalue()),
                        URLError(ssl.SSLCertVerificationError(1, 'certificate'))]
            with patch('keystone.gateway.open_upstream', side_effect=upstream):
                for _ in range(2):
                    with self.assertRaises(ModelStreamInterrupted):
                        client.stream_chat(self.messages, model='test')
                with self.assertRaises(GatewayError) as err:
                    client.stream_chat(self.messages, model='test')
                self.assertNotIsInstance(err.exception, ModelStreamInterrupted)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
