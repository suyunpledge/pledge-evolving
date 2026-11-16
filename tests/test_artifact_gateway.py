"""Gateway actions and action-query receipts share the retained-output store."""
import unittest

import test_runtime_convergence as fixtures
from forge.policy import Policy, Mode, ToolEffect
from forge.tools import ToolSpec, ToolResult, build_builtin_registry


class ArtifactGatewayTests(unittest.TestCase):
    setUp = fixtures.RuntimeConvergenceTests.setUp
    gateway = fixtures.RuntimeConvergenceTests.gateway
    http = fixtures.RuntimeConvergenceTests.http

    def test_read_only_http_retains_queries_and_pages_large_result(self):
        reg = build_builtin_registry()
        reg.register(ToolSpec('large_observation', '', lambda a, c: ToolResult(True,
            'start' + 'x'*8000 + ' tail-needle')), trusted=True, effect=ToolEffect.READ)
        _, base = self.gateway(registry=reg, policy=Policy(mode=Mode.READ_ONLY,
            workspace=self.ws, non_interactive=True))
        before = sorted(p.name for p in self.ws.iterdir())
        body = {'run_id': 'artifact_run', 'action_id': 'long_action',
                'name': 'large_observation', 'arguments': {}}
        code, result = self.http(base, '/v1/tools/call', body)
        self.assertEqual(code, 200)
        self.assertTrue(result['ok'])
        identifier = result['meta']['artifact']['id']
        self.assertLess(len(result['content']), 4000)
        _, receipt = self.http(base, '/v1/runs/artifact_run/actions/long_action')
        self.assertEqual(receipt['meta']['artifact'], result['meta']['artifact'])
        _, page = self.http(base, '/v1/tools/call', {'run_id': 'artifact_run',
            'name': 'artifact_read', 'arguments': {'id': identifier, 'offset': 8000}})
        self.assertTrue(page['ok'])
        self.assertIn('tail-needle', page['content'])
        _, found = self.http(base, '/v1/tools/call', {'run_id': 'artifact_run',
            'name': 'artifact_grep', 'arguments': {'id': identifier, 'pattern': 'tail-needle'}})
        self.assertTrue(found['ok'])
        self.assertEqual(found['meta']['matches'][0]['offset'], 8006)
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), before)

    def test_raw_workspace_write_cannot_overwrite_host_artifacts(self):
        from forge.artifacts import ArtifactStore
        from forge.execution import ActionExecutor, ActionRequest, create_execution_context
        store = ArtifactStore(self.home, self.ws)
        identifier = store.put('original')['id']
        context = create_execution_context(home=self.home, workspace=self.ws,
            policy=Policy(mode=Mode.BYPASS, workspace=self.ws, non_interactive=True),
            registry=build_builtin_registry())
        self.addCleanup(context.secret_scope.close)
        self.addCleanup(context.session.close)
        result = ActionExecutor().execute(context, ActionRequest('write_file', {
            'path': str(store.root / identifier), 'content': 'corrupt'}))
        self.assertEqual(result.authorization, 'deny')
        self.assertEqual(store.read(identifier)['text'], 'original')

    def test_builtin_search_view_and_receipt_retain_plain_source(self):
        for i in range(85):
            (self.ws / f'inventory_inspection_{i:03}.txt').write_text('row', encoding='utf-8')
        _, base = self.gateway(registry=build_builtin_registry(), policy=Policy(mode=Mode.READ_ONLY,
            workspace=self.ws, non_interactive=True))
        code, value = self.http(base, '/v1/tools/call', {'run_id': 'builtin_view',
            'action_id': 'inventory', 'name': 'find_files', 'arguments': {'pattern': '*.txt'}})
        import json
        self.assertEqual(code, 200)
        view = json.loads(value['content'])
        self.assertEqual(view['summary']['files'], 86)  # fixture value.txt + inventory
        self.assertNotIn('_result_view_source', value['meta'])
        identifier = view['_retained']['id']
        _, receipt = self.http(base, '/v1/runs/builtin_view/actions/inventory')
        self.assertEqual(receipt['content'], value['content'])
        _, page = self.http(base, '/v1/tools/call', {'run_id': 'builtin_view',
            'name': 'artifact_grep', 'arguments': {'id': identifier, 'pattern': 'inspection_084'}})
        self.assertTrue(page['ok'])
        self.assertIn('inspection_084.txt', page['content'])


if __name__ == '__main__':
    unittest.main()
