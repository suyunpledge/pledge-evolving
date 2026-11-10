"""Work HTTP and Code editor integration for the local-reference improvements."""
import json
import unittest
from unittest.mock import patch

import test_client_runtime as http_fixtures
import test_development_execution as code_fixtures
from forge.policy import ToolEffect
from forge.test_task_planning import Router
from forge.tools import ToolResult, ToolSpec


class WorkModeEntryTests(unittest.TestCase):
    setUp = http_fixtures.ClientRuntimeTests.setUp
    transport = http_fixtures.ClientRuntimeTests.transport
    use_transport = http_fixtures.ClientRuntimeTests.use_transport
    server = http_fixtures.ClientRuntimeTests.server
    http = http_fixtures.ClientRuntimeTests.http
    wait_terminal = http_fixtures.ClientRuntimeTests.wait_terminal

    def test_work_http_stops_multi_tool_cycle_with_durable_diagnostics(self):
        from forge import loop
        responses = [json.dumps({'tool': f'fixture_lookup_{index % 3}', 'args': {}})
                     for index in range(20)]
        self.use_transport(self.transport(responses))
        build = loop.build_agent
        def with_tools(**kwargs):
            agent = build(**kwargs)
            for index in range(3):
                agent.registry.register(ToolSpec(f'fixture_lookup_{index}', '', lambda a, c:
                    ToolResult(False, error='same failure', meta={'execution_state': 'completed'})),
                    trusted=True, effect=ToolEffect.READ)
            return agent
        with patch.object(loop, 'build_agent', side_effect=with_tools):
            code, started = self.http(self.server(), '/api/run', {'task': 'look up information', 'max_steps': 20})
            self.assertEqual(code, 202)
            record = self.wait_terminal(started['run_id'])
        self.assertEqual(record['state'], 'error')
        self.assertEqual(record['stopped'], 'stalled')
        self.assertEqual(len(record['steps']), 9)
        stop = next(e for e in record['events'] if e['type'] == 'loop_stop')
        self.assertEqual(stop['detection']['period'], 3)


class CodeModeEntryTests(unittest.TestCase):
    setUp = code_fixtures.ExecutionTests.setUp
    runtime = code_fixtures.ExecutionTests.runtime

    def test_code_editor_attached_document_activates_scoped_claude_guidance(self):
        (self.project / 'CLAUDE.md').write_text('ROOT CLAUDE code style', encoding='utf-8')
        (self.project / 'src').mkdir()
        (self.project / 'src' / 'CLAUDE.md').write_text('SCOPED CLAUDE module rule', encoding='utf-8')
        (self.project / 'other').mkdir()
        (self.project / 'other' / 'CLAUDE.md').write_text('UNRELATED RULE', encoding='utf-8')
        router = Router(['done'])
        runtime = self.runtime(router=router)
        result = runtime.run('inspect attached file', permission='read-only', planning='none',
            contexts=[{'path': str(self.project / 'src' / 'new.py'), 'content': 'unsaved buffer'}])
        self.assertTrue(result['ok'])
        messages = router.calls[0][0]
        self.assertIn('ROOT CLAUDE', str(messages))
        self.assertIn('SCOPED CLAUDE', str(messages))
        self.assertNotIn('UNRELATED RULE', str(messages))
        self.assertNotIn('SCOPED CLAUDE', str([m for m in messages if m['role'] == 'system']))


if __name__ == '__main__':
    unittest.main()
