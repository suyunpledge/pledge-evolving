"""Loom design integration: real local Agent/action paths, no paid models."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from .acceptance import AcceptanceContract, verify
from .file_revisions import FileRevisionGuard
from .loop import Agent, LoopLimits
from .model import ModelRouter, Provider, Usage
from .policy import Mode, Policy
from .protocol import run_payload
from .tools import ToolContext, ToolResult, build_builtin_registry


def contract(**check):
    return {'criteria': [{'id': 'output', 'kind': 'file', 'path': 'output.txt', **check}]}


class Scripted:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, provider, model, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        response = next(self.responses)
        if callable(response):
            response = response()
        return response, Usage(prompt_tokens=10, completion_tokens=2, model=model, provider=provider.name)


class LoomIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.path = self.workspace / 'output.txt'

    def agent(self, *responses, **kwargs):
        transport = Scripted(*responses)
        agent = Agent(home=self.root / 'home', workspace=self.workspace,
            registry=build_builtin_registry(),
            router=ModelRouter([Provider('local', 'https://example.invalid', default_model='fake')],
                               transport=transport, retries_per_provider=0),
            policy=Policy(workspace=self.workspace, mode=Mode.ACCEPT_EDITS, non_interactive=True),
            limits=LoopLimits(max_steps=6), project_rules=False, **kwargs)
        return agent, transport

    def check(self, raw, *, execute=None, policy=None, isolated=True, cancel=lambda: None):
        registry = build_builtin_registry()
        context = ToolContext(policy or Policy(workspace=self.workspace, mode=Mode.ACCEPT_EDITS),
                              self.workspace, isolated=isolated)
        self.addCleanup(context.secret_scope.close)
        return verify(AcceptanceContract.from_raw(raw), workspace=self.workspace, registry=registry,
                      context=context, execute=execute or (lambda n, a: registry.invoke(n, a, context)),
                      check_cancel=cancel)

    def test_file_check_reads_beyond_tool_preview(self):
        self.path.write_text('x' * 10000 + '\nverified', encoding='utf-8')
        result = self.check(contract(contains='verified'))
        self.assertTrue(result['passed'])
        self.assertEqual(result['results'][0]['evidence']['bytes'], len(self.path.read_bytes()))

    def test_missing_directory_and_absence_are_not_confused(self):
        self.assertFalse(self.check(contract())['passed'])
        self.assertTrue(self.check(contract(exists=False))['passed'])
        self.path.mkdir()
        self.assertFalse(self.check(contract())['passed'])
        self.assertFalse(self.check(contract(exists=False))['passed'])

    def test_read_deny_wins_even_for_absence(self):
        policy = Policy(workspace=self.workspace, deny=('read_file',), mode=Mode.BYPASS)
        self.assertFalse(self.check(contract(exists=False), policy=policy)['passed'])

    def test_symlink_escape_rejected(self):
        outside = self.root / 'outside.txt'
        outside.write_text('private', encoding='utf-8')
        try:
            self.path.symlink_to(outside)
        except OSError:
            self.skipTest('OS does not allow symlinks')
        self.assertFalse(self.check(contract())['passed'])

    def test_invalid_contracts_fail_before_model(self):
        invalid = [{}, {'criteria': []}, contract(path='../escape'), contract(path='C:\\escape'),
                   contract(exists='false'), contract(kind='unknown'), contract(required=False),
                   contract(contains='x', exists=False), {**contract(), 'max_attempts': 99},
                   {'criteria': contract()['criteria'] * 2}, contract(extra=True),
                   {'criteria': [{'kind': 'file', 'path': 'file'}]}]
        agent, transport = self.agent('unused')
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                agent.run('task', acceptance=raw)
        self.assertFalse(transport.calls)

    def test_command_cannot_bypass_protected_agent(self):
        raw = {'criteria': [{'id': 'cmd', 'kind': 'command', 'command': 'echo done'}]}
        with patch('forge.tools.subprocess.run') as run:
            self.assertFalse(self.check(raw)['passed'])
            run.assert_not_called()

    def test_command_requires_structured_exit_evidence(self):
        raw = {'criteria': [{'id': 'cmd', 'kind': 'command', 'command': 'check', 'expect_exit': 2}]}
        for result, expected in [
            (ToolResult(True, 'exit:2'), False),
            (ToolResult(False, 'exit:2', meta={'exit': 2, 'execution_state': 'completed', 'authorization': 'allow'}), True),
            (ToolResult(True, meta={'exit': 2, 'execution_state': 'unknown', 'authorization': 'allow'}), False),
            (ToolResult(True, meta={'exit': 2, 'execution_state': 'completed', 'authorization': 'deny'}), False)]:
            self.assertEqual(self.check(raw, execute=lambda n, a: result)['passed'], expected)

    def test_unknown_command_stops_remaining_checks(self):
        raw = {'criteria': [{'id': 'cmd', 'kind': 'command', 'command': 'check'}, *contract()['criteria']]}
        result = self.check(raw, execute=lambda n, a: ToolResult(False, meta={'execution_state': 'unknown'}))
        self.assertTrue(result['unknown_outcome'])
        self.assertEqual(len(result['results']), 1)

    def test_command_checks_precede_final_file_checks(self):
        self.path.write_text('ok', encoding='utf-8')
        raw = contract(contains='ok')
        raw['criteria'].append({'id': 'cmd', 'kind': 'command', 'command': 'modify'})
        def execute(name, args):
            self.path.write_text('changed', encoding='utf-8')
            return ToolResult(True, meta={'exit': 0, 'execution_state': 'completed', 'authorization': 'allow'})
        self.assertFalse(self.check(raw, execute=execute)['passed'])

    def test_trusted_command_reports_nonzero_as_completed(self):
        from subprocess import CompletedProcess
        raw = {'criteria': [{'id': 'cmd', 'kind': 'command', 'command': 'echo check', 'expect_exit': 2}]}
        with patch('forge.tools.subprocess.run', return_value=CompletedProcess('echo check', 2, '', 'expected')):
            result = self.check(raw, isolated=False,
                policy=Policy(workspace=self.workspace, mode=Mode.BYPASS, non_interactive=True))
        self.assertTrue(result['passed'])

    def test_optional_failure_does_not_block_required_pass(self):
        self.path.write_text('ok', encoding='utf-8')
        raw = contract()
        raw['criteria'].append({'id': 'optional', 'kind': 'file', 'path': 'absent', 'required': False})
        self.assertTrue(self.check(raw)['passed'])

    def test_real_loop_repairs_before_delivering_and_preserves_prefix(self):
        agent, transport = self.agent('done', json.dumps({'tool': 'write_file', 'args': {
            'path': 'output.txt', 'content': 'verified'}}), 'done')
        report = agent.run('create output', acceptance=contract(contains='verified'))
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(agent.task_contract.status, 'ready_to_deliver')
        checked = [e for e in report.events if e['type'] == 'acceptance_checked']
        self.assertEqual([e['passed'] for e in checked], [False, True])
        for a, b in zip(transport.calls, transport.calls[1:]):
            self.assertEqual(a, b[:len(a)])

    def test_false_final_is_bounded_and_not_success(self):
        agent, transport = self.agent('done', 'done', 'done', 'must not be called')
        report = agent.run('create output', acceptance=contract())
        self.assertEqual(report.stopped, 'acceptance_failed')
        self.assertEqual(report.as_result().outcome, 'failed')
        self.assertEqual(agent.task_contract.status, 'blocked')
        self.assertEqual(len(transport.calls), 3)
        self.assertNotIn('assistant_message', [e['type'] for e in report.events])
        payload = run_payload(report)
        self.assertFalse(payload['acceptance']['passed'])
        self.assertEqual(payload['acceptance']['attempt'], 3)

    def test_contract_does_not_leak_into_next_run(self):
        agent, _ = self.agent('done', 'ordinary chat')
        failed = agent.run('task', acceptance={**contract(), 'max_attempts': 1})
        self.assertEqual(failed.stopped, 'acceptance_failed')
        self.assertEqual(agent.run('hello').stopped, 'final')

    def test_cli_json_and_exit_code_follow_real_verification(self):
        from . import cli
        from .session import Session
        spec = self.root / 'acceptance.json'
        spec.write_text(json.dumps({**contract(contains='ready'), 'max_attempts': 1}), encoding='utf-8')
        for content, expected in [('wrong', 1), ('ready', 0)]:
            self.path.write_text(content, encoding='utf-8')
            agent, _ = self.agent('done')
            agent.session = Session(self.root / (content + '.jsonl'))
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli, 'build_agent', return_value=agent), redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(['run', 'task', '--home', str(self.root / 'home'),
                                 '--workspace', str(self.workspace), '--no-user-layer',
                                 '--acceptance', str(spec), '--json'])
            self.assertEqual(code, expected)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload['ok'], expected == 0)
            self.assertEqual(payload['acceptance']['passed'], expected == 0)
            self.assertIn('acceptance_checked', [e.type for e in agent.session.replay()])

    def test_unknown_verification_outcome_is_not_automatically_retried(self):
        agent, transport = self.agent('done', 'must not run')
        raw = {'criteria': [{'id': 'cmd', 'kind': 'command', 'command': 'check'}]}
        with patch.object(agent, '_invoke_recorded', return_value=ToolResult(False,
                          meta={'execution_state': 'unknown'})) as execute:
            report = agent.run('task', acceptance=raw)
        self.assertEqual(report.stopped, 'unknown_outcome')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_cancellation_during_check_prevents_success(self):
        cancel = threading.Event()
        agent, _ = self.agent(lambda: (cancel.set() or 'done'), cancellation_event=cancel)
        self.path.write_text('ok', encoding='utf-8')
        report = agent.run('task', acceptance=contract())
        self.assertEqual(report.stopped, 'cancelled')

    def test_default_agent_rejects_external_edit_after_read(self):
        self.path.write_text('old', encoding='utf-8')
        def external_edit():
            self.path.write_text('human edit', encoding='utf-8')
            return json.dumps({'tool': 'write_file', 'args': {'path': 'output.txt', 'content': 'lost'}})
        agent, _ = self.agent(json.dumps({'tool': 'read_range', 'args': {'path': 'output.txt', 'start': 1, 'end': 1}}),
                              external_edit, 'stopped')
        report = agent.run('edit output')
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'human edit')
        self.assertEqual(report.steps[1].meta['error_code'], 'revision_conflict')

    def test_default_guard_preserves_legacy_unread_write(self):
        self.path.write_text('old', encoding='utf-8')
        agent, _ = self.agent(json.dumps({'tool': 'write_file', 'args': {'path': 'output.txt', 'content': 'new'}}), 'done')
        self.assertEqual(agent.run('write').stopped, 'final')
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'new')

    def test_conflict_rejects_patch_batch_without_partial_write(self):
        self.path.write_text('old', encoding='utf-8')
        second = self.workspace / 'second.txt'
        second.write_text('old', encoding='utf-8')
        def mutate():
            self.path.write_text('human', encoding='utf-8')
            return json.dumps({'tool': 'apply_patch', 'args': {'patches': [
                {'path': 'second.txt', 'old': 'old', 'new': 'lost'},
                {'path': 'output.txt', 'old': 'human', 'new': 'lost'}]}})
        agent, _ = self.agent(json.dumps({'tool': 'read_file', 'args': {'path': 'output.txt'}}), mutate, 'done')
        report = agent.run('edit two files')
        self.assertEqual(report.steps[1].meta['error_code'], 'revision_conflict')
        self.assertEqual(self.path.read_text(), 'human')
        self.assertEqual(second.read_text(), 'old')

    def test_large_file_revision_limit_matches_read_limit(self):
        self.path.write_text('old\n' + 'x' * (600 * 1024), encoding='utf-8')
        agent, _ = self.agent(json.dumps({'tool': 'read_range', 'args': {'path': 'output.txt', 'end': 1}}),
            json.dumps({'tool': 'edit_file', 'args': {'path': 'output.txt', 'old': 'old', 'new': 'new'}}), 'done')
        report = agent.run('edit first line')
        self.assertEqual(report.steps[1].decision, 'ok')
        self.assertTrue(self.path.read_text().startswith('new'))

    def test_development_import_remains_strict_and_child_drops_observations(self):
        from .development.guard import FileRevisionGuard as LegacyGuard
        self.assertIs(LegacyGuard, FileRevisionGuard)
        guard = LegacyGuard(self.workspace)
        self.path.write_text('old', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Read the existing'):
            with guard.protect('write_file', {'path': 'output.txt'}, None):
                self.fail('unread write was allowed')
        guard.revisions[str(self.path)] = guard.digest(self.path)
        self.assertFalse(guard.fork().revisions)
        self.assertTrue(guard.fork().require_read)


if __name__ == '__main__':
    unittest.main()
