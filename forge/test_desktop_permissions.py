import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from .desktop_permissions import profile_policy, approve_action
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .policy import Policy, Decision, ToolEffect
from .tools import ToolRegistry, ToolSpec, ToolResult


class DesktopPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.calls, self.approvals = [], []
        self.registry = ToolRegistry()
        def write(args, ctx):
            self.calls.append(dict(args))
            ctx.policy.abs_path(args['path']).write_text(args['text'])
            return ToolResult(True, 'written')
        self.registry.register(ToolSpec('write_file', 'test writer', write, read_only=False), trusted=True, effect=ToolEffect.WRITE)
        self.registry.register(ToolSpec('native_probe', 'test native', lambda a, c: ToolResult(True, 'ran'),
            read_only=False, tags=('native',)), trusted=True, effect=ToolEffect.NATIVE)

    def context(self, profile, **kwargs):
        return create_execution_context(home=self.root / 'home', workspace=self.workspace,
            registry=self.registry, policy=profile_policy(Policy(workspace=self.workspace, **kwargs), profile))

    def test_sandbox_confines_writes_and_native_execution(self):
        ctx = self.context('sandbox')
        executor = ActionExecutor()
        self.assertTrue(executor.execute(ctx, ActionRequest('write_file', {'path': 'ok.txt', 'text': 'inside'})).as_tool_result().ok)
        self.assertFalse(executor.execute(ctx, ActionRequest('write_file', {'path': '../outside.txt', 'text': 'no'})).as_tool_result().ok)
        self.assertFalse((self.root / 'outside.txt').exists())
        self.assertFalse(executor.execute(ctx, ActionRequest('native_probe', {})).as_tool_result().ok)

    def test_manual_approval_is_exact_single_action_and_deny_does_not_execute(self):
        ctx = self.context('manual')
        answers = iter([True, False])
        def approve(name, args, cancel):
            self.approvals.append(dict(args))
            return next(answers)
        executor = ActionExecutor(approver=approve, trusted_native=True)
        args = {'path': 'one.txt', 'text': 'one'}
        self.assertTrue(executor.execute(ctx, ActionRequest('write_file', args)).as_tool_result().ok)
        self.assertFalse(executor.execute(ctx, ActionRequest('write_file', args)).as_tool_result().ok)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.approvals), 2)
        self.assertIs(ctx.policy.evaluate('write_file', args=args, effect=ToolEffect.WRITE), Decision.ASK)

    def test_approval_cannot_authorize_changed_arguments_or_explicit_denial(self):
        base = self.context('manual').policy
        approved = approve_action(base, 'write_file', {'path': 'a', 'text': 'approved'})
        self.assertIs(approved.evaluate('write_file', args={'path': 'a', 'text': 'different'}, effect=ToolEffect.WRITE), Decision.ASK)
        denied = self.context('manual', deny=('write_file',))
        executor = ActionExecutor(approver=lambda *_: self.fail('deny must not request approval'))
        self.assertFalse(executor.execute(denied, ActionRequest('write_file', {'path': 'a', 'text': 'x'})).as_tool_result().ok)

    def test_full_access_and_native_grants_remain_host_explicit(self):
        ctx = self.context('full')
        self.assertFalse(ActionExecutor().execute(ctx, ActionRequest('native_probe', {})).as_tool_result().ok)
        executor = ActionExecutor(trusted_native=True)
        self.assertTrue(executor.execute(ctx, ActionRequest('native_probe', {})).as_tool_result().ok)
        self.assertTrue(executor.execute(ctx, ActionRequest('write_file', {'path': '../outside.txt', 'text': 'approved'})).as_tool_result().ok)
        denied = self.context('full', deny=('write_file',))
        self.assertFalse(executor.execute(denied, ActionRequest('write_file', {'path': 'never.txt', 'text': 'x'})).as_tool_result().ok)

    def test_cancel_during_approval_does_not_dispatch(self):
        cancel = threading.Event()
        ctx = replace(self.context('manual'), cancellation=cancel)
        def approve(*_):
            cancel.set()
            return True
        self.assertFalse(ActionExecutor(approver=approve).execute(ctx,
            ActionRequest('write_file', {'path': 'cancelled.txt', 'text': 'x'})).as_tool_result().ok)
        self.assertEqual(self.calls, [])

    def test_delegated_agent_uses_host_approval_without_lifting_its_policy(self):
        from .loop import Agent
        from .model import Completion, Usage
        from .test_vscode_bridge import FakeRouter
        router = FakeRouter()
        replies = iter([
            Completion('', Usage(), tool_calls=[{'id': 'w1', 'name': 'write_file',
                'args': {'path': 'child.txt', 'text': 'approved'}, 'wire': 'openai'},
                {'id': 'n1', 'name': 'native_probe', 'args': {}, 'wire': 'openai'}]),
            Completion('done', Usage())])
        router.complete = lambda *a, **k: next(replies)
        parent = Agent(home=self.root / 'agent', workspace=self.workspace, router=router,
            registry=self.registry, policy=profile_policy(Policy(workspace=self.workspace), 'manual'),
            project_rules=False)
        self.addCleanup(parent.secret_scope.close)
        def approve(name, args, cancel):
            self.approvals.append(name)
            return True
        parent.action_executor.approver = approve
        parent.action_executor.trusted_native = True
        result = parent._spawn_handler('write child file then run native probe', '')
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.workspace / 'child.txt').read_text(), 'approved')
        self.assertEqual(self.approvals, ['write_file', 'native_probe'])
        self.assertIs(parent.policy.evaluate('write_file', args={'path': 'child.txt'}, effect=ToolEffect.WRITE), Decision.ASK)


if __name__ == '__main__':
    unittest.main()
