"""Entry-point integration over real local HTTP, journals, Agents and editor bridge."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_client_runtime as client_fixtures
import test_runtime_convergence as gateway_fixtures
import test_development_execution as editor_fixtures
from client import server as client
from forge.client_runs import RunStore
from forge.test_task_planning import Router


def acceptance(path='result.txt'):
    return {'max_attempts': 1, 'criteria': [
        {'id': 'deliverable', 'kind': 'file', 'path': path, 'contains': 'ready'}]}


class ClientChainTests(unittest.TestCase):
    # Reuse local HTTP/transport fixtures without inheriting their test methods.
    setUp = client_fixtures.ClientRuntimeTests.setUp
    transport = client_fixtures.ClientRuntimeTests.transport
    use_transport = client_fixtures.ClientRuntimeTests.use_transport
    server = client_fixtures.ClientRuntimeTests.server
    http = client_fixtures.ClientRuntimeTests.http
    wait_terminal = client_fixtures.ClientRuntimeTests.wait_terminal

    def test_invalid_acceptance_rejected_before_record_or_worker(self):
        base = self.server()
        for spec in [{}, {'criteria': []}, acceptance('../outside')]:
            code, _ = self.http(base, '/api/run', {'task': 'finish', 'acceptance': spec})
            self.assertEqual(code, 400)
        self.assertFalse(self.store.list_recent())
        self.assertFalse(self.calls)

    def test_http_acceptance_failure_is_durable_and_not_success(self):
        self.use_transport(self.transport(['done']))
        code, started = self.http(self.server(), '/api/run', {'task': 'finish', 'acceptance': acceptance()})
        self.assertEqual(code, 202)
        rec = self.wait_terminal(started['run_id'])
        self.assertEqual(rec['state'], 'error')
        self.assertEqual(rec['error_code'], 'acceptance_failed')
        self.assertFalse(rec['acceptance']['passed'])
        restored = RunStore(self.store.directory)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(rec['id'])['acceptance'], rec['acceptance'])

    def test_http_acceptance_success_and_zero_spawn_limits_reach_agent(self):
        (self.ws / 'result.txt').write_text('ready', encoding='utf-8')
        self.use_transport(self.transport(['done']))
        import forge.loop as loop
        build = loop.build_agent
        captured = []
        def capture(**kwargs):
            captured.append(kwargs['limits'])
            return build(**kwargs)
        with patch.object(loop, 'build_agent', side_effect=capture):
            code, started = self.http(self.server(), '/api/run', {'task': 'finish',
                'acceptance': acceptance(), 'max_depth': 0, 'spawn_budget': 0})
            self.assertEqual(code, 202)
            rec = self.wait_terminal(started['run_id'])
        self.assertEqual(rec['state'], 'done')
        self.assertTrue(rec['acceptance']['passed'])
        self.assertEqual((captured[0].max_depth, captured[0].spawn_budget), (0, 0))


class GatewayChainTests(unittest.TestCase):
    setUp = gateway_fixtures.RuntimeConvergenceTests.setUp
    gateway = gateway_fixtures.RuntimeConvergenceTests.gateway
    http = gateway_fixtures.RuntimeConvergenceTests.http

    def test_gateway_reuses_observations_and_refuses_stale_write(self):
        cfg, base = self.gateway()
        body = {'run_id': 'revision_lane', 'name': 'read_file', 'arguments': {'path': 'value.txt'}}
        self.assertTrue(self.http(base, '/v1/tools/call', body)[1]['ok'])
        self.path.write_text('human edit', encoding='utf-8')
        body.update(name='write_file', arguments={'path': 'value.txt', 'content': 'lost'})
        _, denied = self.http(base, '/v1/tools/call', body)
        self.assertFalse(denied['ok'])
        self.assertEqual(denied['meta']['error_code'], 'revision_conflict')
        self.assertEqual(self.path.read_text(), 'human edit')
        # Another run does not inherit the old observation or silently bypass this lane.
        body['run_id'] = 'new_revision_lane'
        self.assertTrue(self.http(base, '/v1/tools/call', body)[1]['ok'])
        self.assertIsNot(cfg.execution_context('revision_lane').file_guard,
                         cfg.execution_context('new_revision_lane').file_guard)

    def test_reread_refreshes_gateway_observation(self):
        _, base = self.gateway()
        read = {'run_id': 'refresh_lane', 'name': 'read_file', 'arguments': {'path': 'value.txt'}}
        self.assertTrue(self.http(base, '/v1/tools/call', read)[1]['ok'])
        self.path.write_text('human', encoding='utf-8')
        self.assertTrue(self.http(base, '/v1/tools/call', read)[1]['ok'])
        _, written = self.http(base, '/v1/tools/call', {'run_id': 'refresh_lane', 'name': 'write_file',
            'arguments': {'path': 'value.txt', 'content': 'reviewed'}})
        self.assertTrue(written['ok'])


class EditorChainTests(unittest.TestCase):
    setUp = editor_fixtures.ExecutionTests.setUp
    runtime = editor_fixtures.ExecutionTests.runtime

    def test_development_to_editor_to_agent_enforces_acceptance(self):
        runtime = self.runtime(router=Router(['done']))
        result = runtime.run('finish', permission='workspace-write', planning='none',
                             acceptance=acceptance('missing.txt'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['stopped'], 'acceptance_failed')
        self.assertFalse(result['acceptance']['passed'])

    def test_bad_editor_contract_fails_before_emitting_user_or_changing_history(self):
        runtime = self.runtime(router=Router(['must not run']))
        bridge = runtime.bridge
        history = list(bridge.history)
        with self.assertRaises(ValueError):
            bridge.run({'prompt': 'task', 'acceptance': {}})
        self.assertEqual(bridge.history, history)
        self.assertFalse(bridge.router.calls)


class ImportBoundaryTests(unittest.TestCase):
    def test_values_and_turn_import_without_execution_or_vendor_protocols(self):
        result = subprocess.run([sys.executable, '-c',
            'import sys; import forge.runtime_types; import forge.tool_turn; '
            'assert "forge.execution" not in sys.modules; '
            'assert "forge.tools" not in sys.modules; assert "forge.toolwire" not in sys.modules'],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
