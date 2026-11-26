import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from . import opencode
from .gateway import GatewayConfig, serve
from .model import HttpTransport, Provider
from .provider_requests import recent_requests


class OpenCodeRequests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.messages = [{'role': 'user', 'content': 'test'}]

    def upstream(self, req, **_):
        self.sent.append(req)
        if req.full_url.endswith('/messages'):
            data = {'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn'}
        else:
            data = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
        res = io.BytesIO(json.dumps(data).encode())
        res.status = 200
        res.headers = {'Content-Type': 'application/json'}
        return res

    def test_direct_both_protocols_and_scoped_headers(self):
        transport = HttpTransport(response_opener=self.upstream, history_home=self.home)
        cases = [(opencode.GO + '/openai/v1', 'openai', '/chat/completions'),
                 (opencode.ZEN + '/anthropic/v1', 'anthropic', '/messages'),
                 (opencode.ORIGIN + '/zen/go/v1', 'anthropic', '/messages')]
        for base, wire, path in cases:
            with self.subTest(base=base), opencode.conversation('same-conversation'):
                provider = Provider('configured', base, wire=wire, default_model='test')
                transport.complete(provider, 'test', self.messages)
                self.assertEqual(self.sent[-1].full_url, base + path)
                self.assertEqual(recent_requests(self.home)[0]['request_url'], base + path)
        ids = {req.get_header('X-opencode-session') for req in self.sent}
        self.assertEqual(len(ids), 1)
        self.assertRegex(ids.pop(), r'^ses_[0-9a-f]{32}$')
        for url in ('https://opencode.ai.example/inference/go', 'http://opencode.ai/zen/go/v1',
                    'https://opencode.ai/account'):
            self.assertFalse(opencode.request_headers(url, 'local-conversation'))

    def test_gateway_keeps_plan_identity_and_key_across_both_tool_rounds(self):
        captured = []
        def upstream(req, **_):
            payload = json.loads(req.data)
            captured.append((req.full_url, req.get_header('Authorization'), payload['model']))
            if payload['messages'][-1]['role'] == 'tool':
                reply = {'role': 'assistant', 'content': '12'}
            else:
                reply = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c', 'type': 'function',
                         'function': {'name': 'multiply', 'arguments': '{"x":3,"y":4}'}}]}
            choice = {'message': reply, 'finish_reason': 'tool_calls' if 'tool_calls' in reply else 'stop'}
            response = io.BytesIO(json.dumps({'choices': [choice]}).encode())
            response.headers = {'content-type': 'application/json'}
            response.status = 200
            return response
        self.enterContext(patch('revon.gateway.open_upstream', upstream))
        for plan, root in [('go', opencode.GO), ('zen', opencode.ZEN)]:
            rid = 'opencode-' + plan
            base = root + '/openai/v1'
            cfg = GatewayConfig(upstream=base, api_key='fixture-' + plan, upstream_wire='openai', wire='openai',
                                port=0, home=self.home, model_map={rid: 'kimi-k3'}, provider_options={'providerId': rid})
            server = serve(cfg)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(cfg.close_runtime)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            def send(messages):
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                    data=json.dumps({'model': rid, 'messages': messages}).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.load(response)['choices'][0]['message']
            assistant = send(self.messages)
            call, = assistant['tool_calls']
            args = json.loads(call['function']['arguments'])
            result = str(args['x'] * args['y'])
            self.assertEqual(send(self.messages + [assistant, {'role': 'tool', 'tool_call_id': call['id'],
                'content': result}])['content'], result)
            target = base + '/chat/completions'
            self.assertEqual(captured[-2:], [(target, 'Bearer fixture-' + plan, 'kimi-k3')] * 2)
            row = recent_requests(self.home)[0]
            self.assertEqual((row['provider_id'], row['base_url'], row['request_url'], row['attempts']),
                             (rid, base, target, 2))
        self.assertEqual(len(recent_requests(self.home)), 2)

    def test_gateway_preserves_conversation_across_tool_rounds_and_switches(self):
        self.enterContext(patch('revon.gateway.open_upstream', self.upstream))
        base = opencode.GO + '/openai/v1'
        cfg = GatewayConfig(upstream=base, upstream_wire='openai', port=0, home=self.home)
        server = serve(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(cfg.close_runtime)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for sid, path in [('a', 'chat/completions'), ('a', 'messages'), ('b', 'chat/completions')]:
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/{path}',
                data=json.dumps({'model': 'test', 'messages': self.messages, 'max_tokens': 32}).encode(),
                headers={'Content-Type': 'application/json', 'X-Revon-Conversation': sid})
            with urllib.request.urlopen(req, timeout=5) as response:
                self.assertEqual(response.status, 200)
        ids = [req.get_header('X-opencode-session') for req in self.sent]
        self.assertTrue(ids[0])
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], ids[2])
        for req in self.sent:
            self.assertEqual(req.full_url, base + '/chat/completions')
            self.assertTrue(req.get_header('User-agent').startswith('revon/'))
            self.assertIsNone(req.get_header('X-revon-conversation'))
        self.assertEqual(recent_requests(self.home)[0]['attempts'], 3)
