import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from .desktop_permissions import profile_policy
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .policy import Policy, override_warnings
from .tools import build_builtin_registry


class ApprovedChecks(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.ws, self.home = root / 'project', root / 'home'
        self.ws.mkdir()
        (self.ws / 'pyproject.toml').touch()
        (self.ws / 'test_example.py').write_text(
            "import os, unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n"
            " def test_marker(self):\n  Path('ran').touch()\n  self.assertNotIn('MY_TEST_PASSWORD', os.environ)\n  self.assertNotIn('registered-environment-fixture', os.environ.values())\n"
            "  self.assertEqual(2+2,5,'fixture-intentional-failure')\n", encoding='utf8')
        from .secrets import SecretScope
        scope = SecretScope()
        scope.reference('registered-environment-fixture')
        self.addCleanup(scope.close)
        self.enterContext(patch.dict(os.environ, LANG='registered-environment-fixture'))
        self.cards = []
        self.approved = True
        self.enterContext(patch.dict(os.environ, MY_TEST_PASSWORD='fixture-only'))

    def approve(self, _, detail, cancel):
        self.cards.append(detail)
        return self.approved

    def call(self, profile, args, *, interactive=True):
        policy = profile_policy(Policy(workspace=self.ws, allow=('run_check',)), profile)
        self.assertTrue(override_warnings(policy))
        registry = build_builtin_registry()
        flags = {'check_dialog': self.approve, 'check_interactive': interactive}
        ctx = create_execution_context(home=self.home, workspace=self.ws, policy=policy,
            registry=registry, capabilities=flags, cancellation=threading.Event())
        self.addCleanup(ctx.secret_scope.close)
        self.addCleanup(ctx.session.close)
        ctx.registry.search('run tests', ctx=ctx.tool_context())
        return ActionExecutor(approver=self.approve).execute(ctx, ActionRequest('run_check', args))

    def test_real_terminal_requires_a_new_yes_and_headless_has_no_callback(self):
        from types import SimpleNamespace
        from .development.project_check import attach_console
        agent = SimpleNamespace(action_executor=ActionExecutor())
        with patch('sys.stdin.isatty', return_value=False):
            self.assertFalse(attach_console(agent))
            self.assertIsNone(agent.action_executor.approver)
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stderr.isatty', return_value=True), patch('builtins.input', side_effect=['y', '']):
            self.assertTrue(attach_console(agent))
            detail = {'commands': ['test'], 'cwd': str(self.ws), 'timeout_seconds': 300, 'note': 'fixture'}
            self.assertTrue(agent.action_executor.approver('run_check', detail, None))
            self.assertFalse(agent.action_executor.approver('run_check', detail, None))
            self.assertFalse(agent.action_executor.approver('shell_exec', detail, None))

    def test_work_profiles_each_require_an_actual_approval_and_return_failed_tests(self):
        for mode in ('sandbox', 'manual', 'full'):
            with self.subTest(mode=mode):
                result = self.call(mode, {'task': 'test', 'filter': 'test_marker'})
                self.assertIn('fixture-intentional-failure', result.content, result.error)
                self.assertTrue((self.ws / 'ran').is_file())
                self.assertEqual(result.meta['exit_code'], 1)
                self.assertEqual(self.cards[-1]['cwd'], str(self.ws))
                self.assertIn('unittest', self.cards[-1]['commands'][0])
        self.assertEqual(len(self.cards), 3)

    def test_timeout_and_cancellation_stop_an_approved_check(self):
        (self.ws / 'test_example.py').write_text("import time\ntime.sleep(10)\n", encoding='utf8')
        with patch('revon.development.project_check.TIMEOUT', .3):
            result = self.call('full', {'task': 'test'})
        self.assertEqual(result.meta['status'], 'timed_out')
        def stop_after_approval(_, detail, cancel):
            timer = threading.Timer(.25, cancel.set)
            timer.start()
            self.addCleanup(timer.join)
            return True
        self.approve = stop_after_approval
        self.assertEqual(self.call('manual', {'task': 'test'}).meta['status'], 'cancelled')

    def test_headless_refusal_invalid_arguments_and_decline_do_not_launch(self):
        result = self.call('full', {'task': 'test'}, interactive=False)
        self.assertEqual(result.authorization, 'deny')
        self.call('manual', {'task': 'test', 'command': 'unrequested'})
        self.call('manual', {'task': 'test', 'filter': '-unexpected'})
        self.assertFalse(self.cards)
        self.approved = False
        self.call('full', {'task': 'test'})
        self.assertEqual(len(self.cards), 1)
        self.assertFalse((self.ws / 'ran').exists())
