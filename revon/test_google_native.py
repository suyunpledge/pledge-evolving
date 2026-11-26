import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from . import google_native
from .gateway import GatewayConfig, serve
from .model import HttpTransport, Provider
from .provider_requests import recent_requests


def candidate(parts, finish=None):
    choice = {'content': {'role': 'model', 'parts': parts}}
    if finish:
        choice['finishReason'] = finish
    return {'candidates': [choice]}


def sse(*events):
    return b''.join(b'data: ' + json.dumps(event).encode() + b'\r\n\r\n' for event in events)


class GoogleTransportTests(unittest.TestCase):
    base = 'https://generativelanguage.googleapis.com'
    model = 'gemini-test'
    call = {'functionCall': {'name': 'sum', 'args': {'a': 3}}, 'thoughtSignature': 'fixture-sig'}
    messages = [{'role': 'system', 'content': 'test'}, {'role': 'user', 'content': 'three'}]
    tools = [{'type': 'function', 'function': {'name': 'sum', 'parameters': {'type': 'object'}}}]

    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sent = []

    def upstream(self, req, **_):
        self.assertEqual(req.get_header('X-goog-api-key'), 'fixture-key')
        self.assertIsNone(req.get_header('Authorization'))
        payload = json.loads(req.data)
        self.sent.append((req.full_url, payload))
        if len(self.sent) == 1:
            event = candidate([self.call], 'STOP')
        else:
            event = candidate([{'text': 'three'}], 'STOP')
        streaming = req.full_url.endswith('?alt=sse')
        res = io.BytesIO(sse(event) if streaming else json.dumps(event).encode())
        res.status = 200
        res.headers = {'content-type': 'text/event-stream' if streaming else 'application/json'}
        return res

    def test_direct_tool_call_and_signed_replay(self):
        transport = HttpTransport(response_opener=self.upstream, history_home=self.home)
        provider = Provider('google', self.base, api_key='fixture-key', wire='google')
        _, _, extra = transport.complete(provider, self.model, self.messages, tools=self.tools)
        assistant = extra['assistant_message']
        call, = assistant['tool_calls']
        self.assertEqual(call['function']['name'], 'sum')
        reply = {'role': 'tool', 'tool_call_id': call['id'], 'content': '3'}
        text, _, _ = transport.complete(provider, self.model, self.messages + [assistant, reply], tools=self.tools)
        self.assertEqual(text, 'three')
        url, body = self.sent[-1]
        self.assertEqual(url, self.base + '/v1beta/models/gemini-test:generateContent')
        self.assertEqual(body['systemInstruction']['parts'], [{'text': 'test'}])
        self.assertEqual(body['contents'][-2]['parts'], [self.call])
        self.assertEqual(body['contents'][-1]['parts'][0]['functionResponse'], {'name': 'sum', 'response': {'result': '3'}})
        self.assertEqual(body['tools'][0]['functionDeclarations'][0]['parametersJsonSchema'], {'type': 'object'})

    def test_gateway_stream_alias_and_completion(self):
        self.enterContext(patch('revon.gateway.open_upstream', self.upstream))
        cfg = GatewayConfig(upstream=self.base + '/v1beta', upstream_wire='google', wire='openai',
                            api_key='fixture-key', port=0, home=self.home, models=['test-label'],
                            model_map={'test-label': self.model}, provider_options={'providerId': 'google'})
        server = serve(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(cfg.close_runtime)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def send(messages, streaming):
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                  data=json.dumps({'model': 'test-label', 'messages': messages, 'stream': streaming,
                                   'tools': self.tools}).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read()
        data = send(self.messages, True)
        self.assertTrue(data.rstrip().endswith(b'[DONE]'))
        choices = [json.loads(line[6:])['choices'][0] for line in data.splitlines()
                   if line.startswith(b'data: {')]
        self.assertEqual(choices[-1]['finish_reason'], 'tool_calls')
        pieces = [call for choice in choices for call in choice['delta'].get('tool_calls', [])]
        call = pieces[0]
        call['function']['arguments'] = ''.join(piece['function'].get('arguments', '') for piece in pieces)
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
        reply = {'role': 'tool', 'tool_call_id': call['id'], 'content': '3'}
        result = json.loads(send(self.messages + [assistant, reply], False))
        self.assertEqual(result['choices'][0]['message']['content'], 'three')
        self.assertEqual(self.sent[0][0], self.base + '/v1beta/models/gemini-test:streamGenerateContent?alt=sse')
        self.assertEqual(self.sent[1][1]['contents'][-2]['parts'], [self.call])
        records = recent_requests(self.home)
        self.assertEqual([row['provider_id'] for row in records], ['google', 'google'])
        self.assertEqual({row['request_url'] for row in records},
            {self.base + '/v1beta/models/…:' + verb for verb in ('generateContent', 'streamGenerateContent')})

    def test_incomplete_or_filtered_stream_never_releases_an_action(self):
        call = candidate([self.call])
        chunks = google_native.stream_chat(io.BytesIO(sse(call)), self.model)
        self.assertNotIn(b'tool_calls', next(chunks))
        with self.assertRaises(OSError):
            next(chunks)
        for reason in ('MAX_TOKENS', 'SAFETY'):
            body = b''.join(google_native.stream_chat(io.BytesIO(sse(call, candidate([], reason))), self.model))
            self.assertNotIn(b'tool_calls', body)
        with self.assertRaises(ValueError):
            google_native.endpoint(self.base + '/v1beta/openai', self.model)
