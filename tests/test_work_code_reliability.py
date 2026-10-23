"""Mode-specific tool failures: offline dispatch and real test processes."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'forge-gui'))
from work_tools import prepare_calls, invoke
from forge_client import ForgeGatewayClient, GatewayError
from forge.development.testing import execute
from forge.gateway import GatewayConfig, serve
from forge.policy import Policy, Mode
from forge.tools import ToolRegistry, ToolSpec, ToolResult


class WorkCodeReliabilityTests(unittest.TestCase):
    def test_work_bad_arguments_never_dispatch(self):
        for raw in ('{"path":"x","content":"partial', 'not JSON', '[1]', 'NaN'):
            with self.subTest(raw=raw):
                calls = prepare_calls([{'function': {'name': 'write_file', 'arguments': raw}}])
                dispatch = Mock()
                result = invoke(json.loads(calls[0]['function']['arguments']), dispatch)
                self.assertFalse(result['ok'])
                self.assertEqual(result['meta']['execution_state'], 'not_started')
                dispatch.assert_not_called()

    def test_work_duplicate_or_malformed_batch_rejected(self):
        for calls in ([{'function': 'bad'}], [{'id': 'a', 'function': {'name': 'click'}},
                                              {'id': 'a', 'function': {'name': 'type'}}]):
            with self.assertRaises(ValueError): prepare_calls(calls)

    def test_work_receipt_contract_and_unknown_override_success(self):
        for value in (None, 'done', {'ok': 'false'}, {'ok': True, 'meta': []},
                      {'ok': True, 'meta': {'execution_state': 'unknown'}},
                      {'ok': True, 'result': float('nan')}):
            with self.subTest(value=value):
                result = invoke({}, lambda: value)
                self.assertFalse(result['ok'])
                self.assertEqual(result['meta']['execution_state'], 'unknown')

    def test_work_lost_reply_does_not_repeat_action(self):
        invocations = []
        def action():
            invocations.append('clicked')
            raise OSError('lost reply after action')
        result = invoke({}, action)
        self.assertEqual(invocations, ['clicked'])
        self.assertEqual(result['meta']['execution_state'], 'unknown')

    def test_work_known_denial_is_preserved_and_ids_are_paired(self):
        calls = prepare_calls([{'function': {'name': 'inspect', 'arguments': '{}'}}])
        self.assertTrue(calls[0]['id'])
        result = invoke({}, lambda: {'ok': False, 'meta': {
            'execution_state': 'not_started', 'error_code': 'policy_denied'}})
        self.assertEqual(result['meta']['execution_state'], 'not_started')

    def test_gateway_lost_response_has_queryable_identity_without_repost(self):
        client = ForgeGatewayClient()
        requests = []
        def lost(req, **kwargs):
            requests.append(json.loads(req.data))
            raise urllib.error.URLError('receipt lost')
        with patch('forge_client.open_response', side_effect=lost):
            result = invoke({}, lambda: client.call_tool('click', {}))
        self.assertEqual(len(requests), 1)
        self.assertEqual(result['meta']['action_id'], requests[0]['action_id'])
        self.assertEqual(result['meta']['run_id'], requests[0]['run_id'])
        client.secret_scope.close()

    def test_code_pre_cancel_never_launches_but_records_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = threading.Event(); stop.set()
            with patch('forge.development.testing.subprocess.Popen') as launch:
                result = execute([sys.executable, '-c', 'raise SystemExit(99)'], tmp, tmp, stop,
                                 task_id='task', run_id='run')
            launch.assert_not_called()
            receipt = json.loads(Path(result['artifact']).read_text())
            self.assertEqual(receipt['execution_state'], 'not_started')
            self.assertEqual(receipt['status'], 'cancelled')
            self.assertEqual((receipt['task_id'], receipt['run_id']), ('task', 'run'))

    def test_real_gateway_write_with_lost_receipt_can_be_queried_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); workspace = root / 'workspace'; workspace.mkdir()
            performed = []
            def write(args, ctx):
                performed.append(1)
                (ctx.workspace / 'target.txt').write_text('updated')
                return ToolResult(True, 'written')
            registry = ToolRegistry()
            registry.register(ToolSpec('operate', '', write, read_only=False), trusted=True)
            cfg = GatewayConfig(upstream='http://127.0.0.1:1', port=0, home=root / 'control',
                workspace=str(workspace), registry=registry,
                policy=Policy(workspace=workspace, mode=Mode.ACCEPT_EDITS), gateway_token='test-key')
            server = serve(cfg)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            client = ForgeGatewayClient('http://127.0.0.1:' + str(server.server_address[1]), api_key='test-key')
            from forge_client import open_response
            def lose_after_receipt(request, **kw):
                with open_response(request, **kw) as response:
                    response.read()
                raise OSError('reply lost after completion')
            try:
                with patch('forge_client.open_response', side_effect=lose_after_receipt):
                    result = invoke({}, lambda: client.call_tool('operate', {}))
                meta = result['meta']
                req = client._request('GET', f"/v1/runs/{meta['run_id']}/actions/{meta['action_id']}")
                with open_response(req, timeout=5) as response:
                    receipt = json.loads(response.read())
                self.assertEqual(result['meta']['execution_state'], 'unknown')
                self.assertTrue(receipt['ok'])
                self.assertEqual(performed, [1])
                self.assertEqual((workspace / 'target.txt').read_text(), 'updated')
            finally:
                client.secret_scope.close()
                server.shutdown(); server.server_close(); thread.join(5)
                for context in cfg._contexts.values(): context.session.close()

    def test_code_real_failure_and_timeout_are_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            for program, timeout, expected in [('raise SystemExit(3)', 10, 'failed'),
                                               ('import time; time.sleep(10)', .2, 'timed_out')]:
                result = execute([sys.executable, '-B', '-c', program], tmp, tmp, threading.Event(), timeout=timeout)
                self.assertEqual(result['status'], expected)
                self.assertIsNone(result['passed'])
                self.assertTrue(Path(result['artifact']).is_file())

    def test_code_incomplete_output_is_unknown_even_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = Mock(returncode=0)
            process.poll.return_value = 0
            with patch('forge.development.testing.subprocess.Popen', return_value=process), \
                 patch('forge.development.testing.threading.Thread') as thread, \
                 patch('forge.development.testing._stop'):
                thread.return_value.is_alive.return_value = True
                result = execute(['fixture'], tmp, tmp, threading.Event())
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['execution_state'], 'unknown')


if __name__ == '__main__': unittest.main()
