import json
import tempfile
import threading
import unittest
from pathlib import Path
from .permissions import authorize, tool_available
from .conversation import run
from revon_client import ChatMessage, CompletionResult, GenerationCancelled


class DesktopBoundaryTests(unittest.TestCase):
    def start_gateway(self, root):
        from revon.gateway import GatewayConfig, serve
        from revon.policy import Mode, Policy
        from revon.tools import build_builtin_registry
        from revon_client import RevonGatewayClient
        cfg = GatewayConfig(upstream='http://127.0.0.1:1', port=0, home=root / 'home', workspace=str(root),
                            registry=build_builtin_registry(),
                            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=root, non_interactive=True))
        server = serve(cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(cfg.close_runtime)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return RevonGatewayClient(f'http://127.0.0.1:{server.server_address[1]}')

    def test_work_discovery_uses_current_permissions_and_real_gateway_schema(self):
        from .search_service import SearchService
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        client = self.start_gateway(root)
        search = SearchService(root).work_tool()
        for profile in ('sandbox', 'manual'):
            observed = []
            def stream(messages, *, tools, **_):
                declarations = {row['function']['name']: row['function'] for row in tools}
                self.assertEqual(declarations['keystone_grep']['parameters']['required'], ['pattern'])
                self.assertEqual(declarations['keystone_tool_search']['parameters']['required'], ['query'])
                if messages[-1].role == 'tool':
                    result = json.loads(messages[-1].content)
                    self.assertTrue(result['ok'], result)
                    observed.extend(result['meta']['available'])
                    self.assertLessEqual(set(observed), declarations.keys())
                    self.assertNotIn('web_search (already available)', result['content'])
                    self.assertEqual('未开放网页正文' in result['content'], profile == 'sandbox')
                    return CompletionResult('ok')
                if profile == 'manual':
                    self.assertEqual(declarations['keystone_fetch_url']['parameters']['required'], ['url'])
                return CompletionResult('', tool_calls=[{'id': 'lookup', 'type': 'function', 'function': {
                    'name': 'keystone_tool_search', 'arguments': '{"query":"search"}'}}])
            client.stream_chat = stream
            run(client, [ChatMessage('user', '查找搜索工具')], model='test', effort=None, cancel=threading.Event(),
                mcp=({'keystone_public_search': search}, None),
                authorize=lambda n, a, c: authorize(profile, root, n, a, lambda *_: True, c),
                tool_filter=lambda n: tool_available(profile, n), chunk=lambda _: None,
                checkpoint=lambda _: None, status=lambda _: None, next_round=lambda: None)
            self.assertIn('keystone_public_search', observed)
            self.assertEqual('keystone_fetch_url' in observed, profile == 'manual')
            self.assertNotIn('keystone_web_search', observed)

    def test_full_desktop_reports_role_and_gateway_denials_separately(self):
        from revon.agent_templates import AgentTemplate
        from .conversation import preview_receipt
        parent = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = parent / 'workspace'
        root.mkdir()
        outside = parent / 'outside.txt'
        outside.write_text('outside fixture', encoding='utf8')
        client = self.start_gateway(root)
        role = AgentTemplate('test', '工作区角色', '', '', '')
        for template, source in ((role, 'role'), (None, 'gateway')):
            def stream(messages, **kwargs):
                if messages[-1].role == 'tool':
                    result = json.loads(messages[-1].content)
                    self.assertFalse(result['ok'])
                    meta = result['meta']
                    self.assertEqual((meta['policy_source'], meta['desktop_profile']), (source, 'full'))
                    self.assertEqual((meta['error_code'], meta['execution_state']), ('policy_denied', 'not_started'))
                    if template:
                        self.assertEqual(meta['role_name'], role.name)
                    else:
                        self.assertTrue(meta['action_id'])
                        self.assertFalse(meta['read_allows_anywhere'])
                        self.assertIn('网关策略拒绝', preview_receipt('keystone_read_range', result)['detail'])
                    return CompletionResult('done')
                return CompletionResult('', tool_calls=[{'id': 'read', 'type': 'function', 'function': {
                    'name': 'keystone_read_range', 'arguments': json.dumps({'path': str(outside), 'start': 1, 'end': 1})}}])
            client.stream_chat = stream
            run(client, [ChatMessage('user', '检查读取回执')], model='test', effort=None, cancel=threading.Event(),
                authorize=lambda n, a, c: authorize('full', root, n, a, self.fail, c, template=template),
                chunk=lambda _: None, checkpoint=lambda _: None, status=lambda _: None, next_round=lambda: None)

    def test_sandbox_can_page_a_real_retained_result(self):
        from revon.execution import ActionExecutor, ActionRequest, create_execution_context
        from revon.desktop_permissions import profile_policy
        from revon.policy import Policy
        from revon.tools import build_builtin_registry
        from .conversation import model_tool_result
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / 'project'
            workspace.mkdir()
            (workspace / 'example.txt').write_text('x' * 5100 + 'second-page', encoding='utf-8')
            ctx = create_execution_context(home=Path(folder) / 'home', workspace=workspace,
                registry=build_builtin_registry(), policy=profile_policy(Policy(workspace=workspace), 'sandbox'))
            try:
                executor = ActionExecutor()
                def call(name, args):
                    self.assertTrue(tool_available('sandbox', 'keystone_' + name))
                    authorize('sandbox', workspace, 'keystone_' + name, args, self.fail)
                    result = executor.execute(ctx, ActionRequest(name, args)).as_tool_result()
                    self.assertTrue(result.ok, result.error)
                    return result
                result = call('read_file', {'path': 'example.txt'})
                identifier = result.meta['artifact']['id']
                response = {'ok': result.ok, 'content': result.content, 'meta': result.meta}
                hidden = model_tool_result('keystone_read_file', response, ())
                self.assertNotIn('artifact_read', hidden['content'])
                self.assertIn('Narrow', hidden['content'])
                self.assertIn('second-page', call('artifact_read', {'id': identifier, 'offset': 5100}).content)
                self.assertIn('5100', call('artifact_grep', {'id': identifier, 'pattern': 'second-page'}).content)
            finally:
                ctx.session.close()
                ctx.secret_scope.close()

    def test_sandbox_paths_and_manual_or_full_host_grant(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'
            root.mkdir()
            approved = []
            def approve(name, args, cancel):
                approved.append(name)
                return True
            authorize('sandbox', root, 'write_file', {'path': 'in.txt'}, approve)
            authorize('sandbox', root, 'keystone_write_file', {'path': 'in.txt'}, approve)
            authorize('sandbox', root, 'keystone_read_file', {'path': 'in.txt'}, approve)
            authorize('sandbox', root, 'tool_search', {}, approve)
            for name, args in [('write_file', {'path': '../out.txt'}), ('read_file', {'path': '../out.txt'}),
                               ('apply_patch', {'patches': [{'path': '../out.txt'}]}),
                               ('shell_exec', {'command': 'echo hi'}), ('connector_post', {}),
                               ('keystone_shell_exec', {'command': 'echo hi'}),
                               ('keystone_write_file', {'path': '../out.txt'}),
                               ('keystone_keystone_write_file', {'path': 'in.txt'})]:
                with self.assertRaises(PermissionError, msg=name):
                    authorize('sandbox', root, name, args, approve)
            self.assertFalse(approved)
            authorize('manual', root, 'connector_post', {}, approve)
            self.assertEqual(approved, ['connector_post'])
            authorize('full', root, 'connector_post', {}, approve)
            authorize('full', root, 'write_file', {'path': '../out.txt'}, approve)
            self.assertEqual(len(approved), 1)
            with self.assertRaises(PermissionError):
                authorize('manual', root, 'write_file', {'path': 'a'}, lambda *_: False)

    def test_public_search_follows_desktop_permissions(self):
        calls = []
        approved = lambda *args: calls.append(args) or True
        args = {'query': 'revon public search', 'engine': 'github'}
        authorize('sandbox', Path.cwd(), 'keystone_public_search', args, approved)
        self.assertFalse(calls)
        authorize('manual', Path.cwd(), 'keystone_public_search', args, approved)
        self.assertEqual(calls[0][0], 'public_search')

    def test_sandbox_advertises_calculator_but_hides_unavailable_tools(self):
        available = ['keystone_calculator', 'keystone_list_dir', 'keystone_7zip_extract']
        unavailable = ['keystone_shell_exec', 'keystone_comfyui_run', 'keystone_keystone_write_file']
        class Client:
            def list_tools(self, **_):
                return [{'type': 'function', 'function': {'name': name}} for name in available + unavailable]
            def stream_chat(self, _messages, *, tools, **_):
                observed.extend(row['function']['name'] for row in tools)
                return CompletionResult('ok')
        observed = []
        cancelled = threading.Event()
        def approve(*_):
            self.fail('calculator should not ask for approval')
        def sandbox(name, arguments, cancel):
            authorize('sandbox', Path.cwd(), name, arguments, approve, cancel)
        run(Client(), [ChatMessage('user', 'test')], model='test', effort=None, cancel=cancelled,
            authorize=sandbox, tool_filter=lambda name: tool_available('sandbox', name),
            chunk=lambda _: None, checkpoint=lambda _: None, status=lambda _: None, next_round=lambda: None)
        # Declarations are sorted by name so the request prefix stays stable for prompt caching.
        self.assertEqual(observed, sorted(available))
        sandbox('keystone_calculator', {'expression': '1+2'}, cancelled)
        with self.assertRaises(ValueError):
            sandbox('keystone_apply_patch', {'patches': 7}, cancelled)

    def test_cancelling_a_batch_completes_protocol_without_more_dispatch(self):
        cancel, checkpoints, executions = threading.Event(), [], []
        class Client:
            def list_tools(self, **_):
                return [{'type': 'function', 'function': {'name': 'keystone_write_file'}}]
            def stream_chat(self, *_args, **_kwargs):
                return CompletionResult('', tool_calls=[{'id': 'a' + str(i), 'type': 'function',
                    'function': {'name': 'write_file', 'arguments': '{"path":"a.txt"}'}} for i in range(2)])
            def call_tool(self, name, args): executions.append(name)
        def approve(*_): cancel.set()
        with self.assertRaises(GenerationCancelled):
            run(Client(), [ChatMessage('user', 'test')], model='test', effort=None, cancel=cancel,
                authorize=approve, chunk=lambda _: None, checkpoint=checkpoints.append,
                status=lambda _: None, next_round=lambda: None)
        self.assertFalse(executions)
        self.assertEqual([m.role for m in checkpoints[0]], ['user', 'assistant', 'tool', 'tool'])


if __name__ == '__main__': unittest.main()
