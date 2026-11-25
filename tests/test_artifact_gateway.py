"""Gateway actions and action-query receipts share the retained-output store."""
import unittest

import test_runtime_convergence as fixtures
from keystone.policy import Policy, Mode, ToolEffect
from keystone.tools import ToolSpec, ToolResult, build_builtin_registry


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
        _, schema = self.http(base, '/v1/tools')
        functions = {row['function']['name']: row['function'] for row in schema['data']}
        for reader, required in [('keystone_artifact_read', ['id']), ('keystone_artifact_grep', ['id', 'pattern'])]:
            self.assertIn(reader, functions['keystone_grep']['description'])
            self.assertEqual(functions[reader]['parameters']['required'], required)
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
        from keystone.artifacts import ArtifactStore
        from keystone.execution import ActionExecutor, ActionRequest, create_execution_context
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

    def test_gateway_source_read_edit_round_trip_and_placeholder_rejection(self):
        path = self.ws / 'settings.py'
        source = "token = args.get('credential')\npassword = cfg.password\n"
        path.write_text(source, encoding='utf-8')
        _, base = self.gateway()

        def call(name, arguments):
            _, response = self.http(base, '/v1/tools/call', {'run_id': 'code_roundtrip',
                'name': name, 'arguments': arguments})
            return response

        result = call('read_range', {'path': path.name, 'start': 1, 'end': 2})
        self.assertTrue(result['ok'], result)
        rows = [line.split('\t', 1)[1] for line in result['content'].splitlines()]
        self.assertEqual(rows, source.splitlines())
        updated = call('edit_file', {'path': path.name, 'old': rows[0], 'new': "token = args.get('session')"})
        self.assertTrue(updated['ok'], updated)
        self.assertIn("args.get('session')", path.read_text(encoding='utf-8'))
        denied = call('write_file', {'path': path.name, 'content': 'token = {{SECRET_REF:placeholder}}'})
        self.assertFalse(denied['ok'])
        self.assertNotIn('SECRET_REF', path.read_text(encoding='utf-8'))

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
