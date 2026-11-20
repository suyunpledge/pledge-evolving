"""Offline checks for Android bridge credentials, pairing, and assistant replies."""
import importlib.util
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

BRIDGE = Path(__file__).resolve().parents[1] / 'bridge'
sys.path.insert(0, str(BRIDGE))

import bridge_vault
import mcp_connectors
import remote_client
from forge.runtime_types import RunReport


def remote_host_module():
    spec = importlib.util.spec_from_file_location('remote_host_fixture', BRIDGE / 'remote-host.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BridgeSecurityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_legacy_connector_credentials_are_encrypted_and_restorable(self):
        path = self.root / 'mcp_connectors.json'
        legacy = {'test': {'transport': 'http', 'url': 'https://fixture.invalid/mcp',
                           'env': {'AUTH_TOKEN': 'fixture-credential'}}}
        path.write_text(json.dumps(legacy), encoding='utf-8')
        with patch.object(mcp_connectors, 'CONFIG_PATH', path), \
                patch.object(bridge_vault, '_KEY', Fernet.generate_key().decode()):
            self.assertEqual(mcp_connectors.load_config(), legacy)
            encrypted = path.read_bytes()
            self.assertNotIn(b'fixture-credential', encrypted)
            self.assertNotIn(b'fixture.invalid', encrypted)
            self.assertEqual(mcp_connectors.load_config(), legacy)
            path.write_bytes(encrypted[:-8])
            with self.assertRaises(bridge_vault.VaultError):
                mcp_connectors.load_config()

    def test_unconfigured_standalone_vault_is_memory_only(self):
        path = self.root / 'mcp_connectors.json'
        with patch.object(bridge_vault, '_KEY', ''), patch.object(bridge_vault, '_memory', {}):
            bridge_vault.save(path, {'test': {'env': {'AUTH_TOKEN': 'fixture'}}})
            self.assertFalse(path.exists())
            self.assertIn('test', bridge_vault.load(path))

    def test_pair_is_single_use_and_reconnect_requires_its_credential(self):
        module = remote_host_module()
        host = module.RemoteHost(self.root, self.root, self.root)
        code = host.new_pair_code()
        token = host.pair('phone-fixture', code)
        self.assertTrue(host.authenticated('phone-fixture', token))
        self.assertFalse(host.authenticated('phone-fixture', ''))
        self.assertFalse(host.pair('second-phone', code))
        host.reset_auth()
        self.assertFalse(host.authenticated('phone-fixture', token))
        code = host.pair_code
        for _ in range(8):
            self.assertFalse(host.pair('phone-fixture', 'wrong!'))
        self.assertFalse(host.pair('phone-fixture', code))
        self.assertTrue(remote_client._desktop_uri('example.org', 8765).startswith('wss://'))
        self.assertTrue(remote_client._desktop_uri('localhost', 8765).startswith('ws://127.0.0.1:'))

    def test_assistant_http_returns_the_run_report_text(self):
        token = 'fixture-bridge-token-for-local-test-only'
        options = {'FORGE_HOME': str(self.root), 'FORGE_WORKSPACE': str(self.root),
                   'FORGE_BRIDGE_TOKEN': token}
        with patch.dict('os.environ', options), patch.object(mcp_connectors, 'CONFIG_PATH', self.root / 'mcp.json'):
            import forge_bridge
        agent = SimpleNamespace(session=nullcontext(),
                                run=lambda *args, **kwargs: RunReport('fixture reply', [], {}))
        with patch.object(forge_bridge, '_sessions', {'assistant-default': agent}), TestClient(forge_bridge.app) as client:
            result = client.post('/assistant/ask', json={'task': 'fixture'},
                                 headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {'ok': True, 'answer': 'fixture reply', 'steps': 0, 'cost': 0.0})


class RemoteSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_can_run_and_receive_an_explicit_approval(self):
        import websockets

        module = remote_host_module()
        ready = asyncio.get_running_loop().create_future()
        serve = websockets.serve

        @asynccontextmanager
        async def bind(*args, **kwargs):
            async with serve(*args, **kwargs) as server:
                ready.set_result(server.sockets[0].getsockname()[1])
                yield server

        computer = SimpleNamespace(approve=None)

        def fixture_run(*args, **kwargs):
            approved = computer.approve('fixture', {'action': 'fixture'})
            return RunReport('approved' if approved else 'denied', [], {})

        agent = SimpleNamespace(session=nullcontext(), run=fixture_run, _computer_session=computer)
        replies = []

        async def emit(event):
            if event['type'] == 'approval.request':
                remote_client._PENDING[event['request_id']].set_result(True)
            if event['type'] == 'run_done':
                replies.append(event['answer'])

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(module, '_show_code_dialog'), \
                patch.object(module.RemoteHost, 'agent_for', return_value=agent), \
                patch.object(websockets, 'serve', bind), \
                patch.object(remote_client, 'CONFIG_PATH', Path(tmp) / 'pair.json'), \
                patch.object(remote_client, '_state', {'mode': 'local'}), \
                patch.object(bridge_vault, '_KEY', Fernet.generate_key().decode()), \
                patch.dict('os.environ', {'FORGE_REMOTE_TEST_CODE': '424242'}):
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'requires TLS'):
                await module.serve('0.0.0.0', 0, root, root, root)
            server = asyncio.create_task(module.serve('127.0.0.1', 0, root, root, root))
            try:
                port = await asyncio.wait_for(ready, timeout=5)
                result = await remote_client.pair('127.0.0.1', port, '424242')
                self.assertTrue(result['ok'], result)
                await asyncio.wait_for(remote_client.DesktopLink(emit).run('fixture', []), timeout=5)
                self.assertEqual(replies, ['approved'])
            finally:
                server.cancel()
                await asyncio.gather(server, return_exceptions=True)
