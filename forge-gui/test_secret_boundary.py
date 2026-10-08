"""Desktop/plugin secret boundary tests; no credentials/network/native workers."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.secrets import SecretScope, protect_store_path
from forge_client import ChatMessage, ForgeGatewayClient


class DesktopSecretBoundaryTests(unittest.TestCase):
    KEY = 'sk-desktop-synthetic-security-1234567890'

    def test_native_plugin_default_does_not_even_import(self):
        from plugin_runtime import PluginRuntime
        plugin = SimpleNamespace(id='evil', installed=True, needs_ack=False, enabled=True, executes_code=True)
        runtime = PluginRuntime(SimpleNamespace(catalog=lambda: [plugin]))
        with patch.object(runtime, '_load_plugin_module') as load:
            report = runtime.reload()
        load.assert_not_called()
        self.assertEqual(runtime.names(), [])
        self.assertTrue(report.skipped)
        self.assertFalse(runtime.call('resolve_secret', {})['ok'])
        with self.assertRaises(PermissionError): runtime._run_job(plugin, None, 'call', arguments={})

    def test_attachment_protects_and_store_cannot_be_attached(self):
        from interaction_model import read_attachment, compose_prompt
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('API_KEY=' + self.KEY)
            attachment = read_attachment(path)
            self.assertEqual(attachment['protected_secrets'], 1)
            self.assertNotIn(self.KEY, compose_prompt('read this', [attachment]))
            protect_store_path(path)
            with self.assertRaises(PermissionError): read_attachment(path)

    def test_gui_payload_virtualizes_and_session_rotates(self):
        client = ForgeGatewayClient('http://127.0.0.1:1')
        request = client._request('POST', '/v1/chat/completions', {'messages': [
            ChatMessage('user', 'PASSWORD=' + self.KEY).to_dict(client.secret_scope)]})
        first = request.data.decode()
        self.assertNotIn(self.KEY, first)
        self.assertIn('{{SECRET_REF:', first)
        old = client.secret_session
        client.reset_secret_session()
        second = client._request('POST', '/v1/chat/completions', {'messages': [
            ChatMessage('user', 'PASSWORD=' + self.KEY).to_dict(client.secret_scope)]}).data.decode()
        self.assertNotEqual(first, second)
        self.assertNotEqual(old, client.secret_session)

    def test_chat_history_and_error_are_redacted(self):
        from forge_client import GatewayError
        SecretScope().reference(self.KEY)
        self.assertNotIn(self.KEY, json.dumps(ChatMessage('assistant', self.KEY,
                            reasoning_content=self.KEY, tool_calls=[{'arguments': self.KEY}]).to_dict()))
        self.assertNotIn(self.KEY, str(GatewayError(self.KEY)))

    def test_split_stream_callback_never_receives_key(self):
        SecretScope().reference(self.KEY)
        events = [{'choices': [{'delta': {'content': piece}, 'finish_reason': None}]}
                  for piece in (self.KEY[:3], self.KEY[3:16], self.KEY[16:])]
        events.append({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})
        wire = b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events) + b'data: [DONE]\n\n'
        chunks = []
        with patch('forge_client.open_response', return_value=io.BytesIO(wire)):
            reply = ForgeGatewayClient('http://127.0.0.1:1').stream_chat([], on_chunk=chunks.append)
        self.assertEqual(reply.text, '[REDACTED_SECRET]')
        self.assertEqual(''.join(chunks), '[REDACTED_SECRET]')

    def test_subagent_payload_and_response_use_the_boundary(self):
        import sub_agent
        captured = []
        def send(request, **kwargs):
            captured.append(request.data.decode())
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.KEY)
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': self.KEY}}]}).encode())
        provider = {'wire': 'openai', 'baseURL': 'https://vendor.example/v1', 'apiKey': self.KEY, 'model': 'm'}
        with patch('sub_agent.open_response', side_effect=send):
            result = sub_agent.provider_chat(provider, {}, [{'role': 'user', 'content': 'PASSWORD=' + self.KEY}],
                model='m', temperature=.3, max_tokens=16, timeout_s=1)
        self.assertNotIn(self.KEY, captured[0])
        self.assertEqual(result, '[REDACTED_SECRET]')

    def test_gui_fixed_mask_has_no_prefix_suffix_or_length(self):
        from forge_gui_v2 import ForgeGuiApp
        self.assertEqual(ForgeGuiApp._masked_key(None, self.KEY), ForgeGuiApp._masked_key(None, 'tiny'))
        self.assertNotIn(self.KEY[:6], ForgeGuiApp._masked_key(None, self.KEY))
        self.assertIn('Protected', ForgeGuiApp._masked_key(None, self.KEY))
