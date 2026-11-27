import io
import json
import threading
import unittest
from unittest.mock import patch

from revon_client import ChatMessage, CompletionResult, RevonGatewayClient
from .conversation import direct_svg_provider, run, standalone_svg_drawing


class DrawingTests(unittest.TestCase):
    def test_new_drawing_and_user_intent_boundaries(self):
        for text in ('用 SVG 动画画一个自行车骑鹈鹕', '用 SVG 画一只鸭子', 'Please draw a spinning gear in SVG.'):
            self.assertTrue(standalone_svg_drawing([ChatMessage('user', text)]))
        for text in ('解释 SVG 动画', 'Draw a diagram based on an existing SVG',
                     '用 SVG 画一个车轮，只给代码', '修改 logo.svg', '生成 PNG'):
            self.assertFalse(standalone_svg_drawing([ChatMessage('user', text)]))
        request = ChatMessage('user', 'Draw an SVG icon')
        self.assertFalse(standalone_svg_drawing([ChatMessage('assistant', 'previous'), request]))
        request.attachment_names = ('reference.png',)
        self.assertFalse(standalone_svg_drawing([request]))

    def test_selected_route_and_provider_settings(self):
        route = {'baseURL': 'https://token-plan-cn.xiaomimimo.com/v1', 'model': 'mimo-v2.6-pro'}
        self.assertTrue(direct_svg_provider(route))
        for change in ({'adapt': False}, {'reasoningMode': 'on'}, {'thinkBudget': 8192},
                       {'baseURL': 'https://relay.example/v1'}, {'model': 'custom'}, {'wire': 'google'}):
            self.assertFalse(direct_svg_provider({**route, **change}), change)

    def test_turn_instruction_obeys_available_tools_and_remains_transient(self):
        for writable, effort, expected in ((True, None, True), (False, None, False), (True, 'high', False)):
            calls = []
            class Client:
                def list_tools(self, **opts):
                    return [{'type': 'function', 'function': {'name': 'keystone_write_file'}}]

                def stream_chat(self, messages, **opts):
                    calls.append((messages, opts))
                    return CompletionResult('ok')

            history = [ChatMessage('user', 'Draw an SVG icon')]
            run(Client(), history, model='test', effort=effort, cancel=threading.Event(),
                tool_filter=lambda _: writable, authorize=lambda *_: None, direct_svg=True,
                chunk=lambda _: None, checkpoint=lambda _: None, status=lambda _: None, next_round=lambda: None)
            sent, options = calls[0]
            self.assertEqual(options.get('disable_thinking', False), expected)
            self.assertEqual(any('独立 SVG 绘制' in m.content for m in sent), expected)
            self.assertFalse(any('独立 SVG 绘制' in m.content for m in history))

    def test_wire_preserves_user_effort_and_default(self):
        client = RevonGatewayClient('http://127.0.0.1:1')
        self.addCleanup(client.secret_scope.close)
        for disable, effort, expected in ((True, None, {'type': 'disabled'}),
                                         (False, None, None), (True, 'high', None)):
            with patch('revon_client.open_response', return_value=io.BytesIO(b'data: [DONE]\n\n')) as sent:
                client.stream_chat([], disable_thinking=disable, reasoning_effort=effort)
            body = json.loads(sent.call_args.args[0].data)
            self.assertEqual(body.get('thinking'), expected)
            self.assertEqual(body.get('reasoning_effort'), effort)
