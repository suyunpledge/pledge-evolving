"""Environment probe acceptance across real CLI/HTTP/editor entry points."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import threading
import unittest
from unittest.mock import patch

import test_client_runtime as http_fixtures
import test_development_execution as editor_fixtures
from client import server as client
from forge import cli
from forge.probes import ProbeRegistry
from forge.test_task_planning import Router
from forge.test_env_probes import contract


class HTTPProbeTests(unittest.TestCase):
    setUp = http_fixtures.ClientRuntimeTests.setUp
    transport = http_fixtures.ClientRuntimeTests.transport
    use_transport = http_fixtures.ClientRuntimeTests.use_transport
    http = http_fixtures.ClientRuntimeTests.http
    wait_terminal = http_fixtures.ClientRuntimeTests.wait_terminal

    def server(self, probes=None):
        server = client.ClientServer(('127.0.0.1', 0), workspace=self.ws, store=self.store, probes=probes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:' + str(server.server_address[1])

    def test_http_builtin_observes_real_state_and_persists_failure(self):
        self.use_transport(self.transport(['ready', 'ready']))
        path = self.ws / 'result.json'
        base = self.server()
        for value, state in [(False, 'error'), (True, 'done')]:
            path.write_text(json.dumps({'ready': value}), encoding='utf-8')
            code, started = self.http(base, '/api/run', {'task': 'finish', 'acceptance': contract()})
            self.assertEqual(code, 202)
            rec = self.wait_terminal(started['run_id'])
            self.assertEqual(rec['state'], state)
            self.assertEqual(rec['acceptance']['passed'], value)
            self.assertIs(rec['acceptance']['results'][0]['evidence']['actual'], value)

    def test_http_custom_probe_comes_from_server_host_not_payload(self):
        observations = []
        probes = ProbeRegistry.defaults().with_probe('application', 'state',
            lambda ctx, value: observations.append(ctx.run_id) or {'saved': True})
        self.use_transport(self.transport(['done']))
        code, started = self.http(self.server(probes), '/api/run', {'task': 'finish',
            'acceptance': contract(env='application', probe='state', input={}, expect={'saved': True})})
        self.assertEqual(code, 202)
        rec = self.wait_terminal(started['run_id'])
        self.assertEqual(rec['state'], 'done')
        self.assertEqual(observations, [started['run_id']])

    def test_cli_builtin_enforces_probe_and_json_result(self):
        # CLI uses its own configured router; inject only the offline transport route.
        transport = self.transport(['done'])
        smart_factory = cli.SmartRouter.from_config
        model_factory = cli.ModelRouter.from_config
        spec = self.root / 'probe.json'
        spec.write_text(json.dumps(contract()), encoding='utf-8')
        (self.ws / 'result.json').write_text('{"ready":false}', encoding='utf-8')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('forge.cli.SmartRouter.from_config', side_effect=lambda cfg, **kwargs: smart_factory(cfg, transport=transport)), \
             patch('forge.cli.ModelRouter.from_config', side_effect=lambda cfg, **kwargs: model_factory(cfg, transport=transport)), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(['run', 'finish', '--home', str(self.root / 'cli-home'),
                '--workspace', str(self.ws), '--no-user-layer', '--acceptance', str(spec), '--json'])
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['stopped'], 'acceptance_failed')
        self.assertIs(payload['acceptance']['results'][0]['evidence']['actual'], False)


class EditorProbeTests(unittest.TestCase):
    setUp = editor_fixtures.ExecutionTests.setUp
    runtime = editor_fixtures.ExecutionTests.runtime

    def test_development_editor_agent_probe_end_to_end(self):
        runtime = self.runtime(router=Router(['done']))
        (self.project / 'result.json').write_text('{"ready":false}', encoding='utf-8')
        result = runtime.run('finish', permission='workspace-write', planning='none', acceptance=contract())
        self.assertFalse(result['ok'])
        self.assertEqual(result['stopped'], 'acceptance_failed')
        self.assertIs(result['acceptance']['results'][0]['evidence']['actual'], False)

    def test_host_probe_registry_survives_development_bridge_assembly(self):
        from forge.development.runtime import DevelopmentRuntime
        from forge.development.state import ModeState
        observed = []
        probes = ProbeRegistry.defaults().with_probe('application', 'saved',
            lambda ctx, value: observed.append(ctx.run_id) or {'persisted': True})
        runtime = DevelopmentRuntime(ModeState(self.root / 'probe-home'), probes=probes)
        self.addCleanup(runtime.close)
        runtime.initialize(self.project)
        runtime.state.switch('development')
        runtime.bridge.router = Router(['done'])
        runtime.bridge.agent.thinking_mode = 'off'
        runtime.bridge.agent.thinking = False
        result = runtime.run('finish', permission='workspace-write', planning='none', acceptance=contract(
            env='application', probe='saved', input={}, expect={'persisted': True}))
        self.assertTrue(result['ok'])
        self.assertEqual(observed, [result['messageId']])


if __name__ == '__main__':
    unittest.main()
