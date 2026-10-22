"""Offline Agent loops with real tools/files/journals and provider-shaped replies."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge.checkpoint import CheckpointStore
from forge.development.guard import FileRevisionGuard
from forge.execution import ActionExecutor
from forge.loop import Agent, LoopLimits
from forge.model import Completion, Usage, TransportError
from forge.policy import Mode, Policy
from forge.session import Session
from forge.tool_adapter import normalize_text_call, repair_arguments
from forge.tools import ToolResult, ToolSpec, build_builtin_registry
from forge import toolwire


class Router:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, Completion) else Completion(reply, Usage(1, 1, 'offline', 'test'))


def native(rows, wire='openai', original=None):
    return Completion('', Usage(1, 1, 'offline', 'test'), tool_calls=rows,
                      wire=wire, assistant_message=original)


def text_call(name, args):
    return '<tool_call>' + json.dumps({'tool': name, 'args': args}) + '</tool_call>'


class AgentStabilityTests(unittest.TestCase):
    def agent(self, replies, registry=None, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        workspace = root / 'project'
        workspace.mkdir()
        router = Router(replies)
        agent = Agent(home=root / 'control', workspace=workspace, router=router,
                      registry=registry or build_builtin_registry(),
                      policy=Policy(workspace=workspace, mode=Mode.ACCEPT_EDITS,
                                    allow=('spawn_subagent',), non_interactive=True),
                      limits=LoopLimits(max_steps=8), **kwargs)
        self.addCleanup(agent.secret_scope.close)
        return agent, router

    def test_wrapped_bad_arguments_never_become_default_arguments(self):
        for bad in ('{broken', ['not-an-object'], '[{"path":"x"}]', 42, True):
            with self.subTest(bad=bad):
                name, args = normalize_text_call({'tool': 'list_dir', 'args': bad})
                self.assertEqual(name, 'list_dir')
                self.assertIn('_raw', args)

    def test_outer_truncation_marker_survives_argument_unwrapping(self):
        name, args = normalize_text_call({'tool': 'write_file', 'args': {'path': 'x'}, '_truncated': True})
        self.assertTrue(args['_truncated'])

    def test_incomplete_native_write_rejected_then_complete_retry_succeeds(self):
        incomplete = repair_arguments('{"path":"value.txt","content":"partial')
        agent, router = self.agent([
            native([{'id': 'first', 'name': 'write_file', 'args': incomplete}]),
            native([{'id': 'retry', 'name': 'write_file', 'args': {'path': 'value.txt', 'content': 'complete'}}]),
            'done'])
        report = agent.run('Create value.txt')
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(report.steps[0].meta['error_code'], 'invalid_arguments')
        self.assertEqual(report.steps[0].meta['execution_state'], 'not_started')
        self.assertIn('complete JSON object', str(router.requests[1]))
        self.assertEqual((agent.workspace / 'value.txt').read_text(), 'complete')
        self.assertNotEqual(report.steps[0].action_id, report.steps[1].action_id)

    def test_bad_wrapped_text_arguments_never_reach_handler(self):
        registry = build_builtin_registry()
        invoked = []
        registry.register(ToolSpec('defaults', '', lambda a, c: invoked.append(a) or ToolResult(True)), trusted=True)
        agent, _ = self.agent([text_call('defaults', '{bad'), 'done'], registry)
        report = agent.run('inspect')
        self.assertFalse(invoked)
        self.assertEqual(report.steps[0].meta['error_code'], 'invalid_arguments')

    def test_complete_tolerated_syntax_and_flattened_arguments_still_work(self):
        self.assertEqual(repair_arguments("{'path': 'value.txt',}"), {'path': 'value.txt'})
        self.assertEqual(normalize_text_call({'tool': 'read_file', 'path': 'x'}), ('read_file', {'path': 'x'}))
        self.assertEqual(normalize_text_call({'tool': 'list_dir', 'args': None}), ('list_dir', {}))

    def test_unknown_write_stops_batch_and_next_model_request(self):
        for protocol in ('text', 'openai', 'anthropic'):
            with self.subTest(protocol=protocol):
                registry = build_builtin_registry()
                invocations = []
                def partial(a, ctx):
                    invocations.append(a)
                    (ctx.workspace / 'partial.txt').write_text('partial')
                    raise OSError('failed after write')
                registry.register(ToolSpec('partial_write', '', partial, read_only=False), trusted=True)
                rows = [{'id': 'one', 'name': 'partial_write', 'args': {}},
                        {'id': 'two', 'name': 'write_file', 'args': {'path': 'later.txt', 'content': 'bad'}}]
                reply = (''.join(text_call(r['name'], r['args']) for r in rows)
                         if protocol == 'text' else native(rows, protocol))
                agent, router = self.agent([reply, reply, 'done'], registry)
                report = agent.run('perform actions')
                self.assertEqual(report.stopped, 'unknown_outcome')
                self.assertEqual(report.as_result().outcome, 'unknown')
                self.assertEqual(len(invocations), 1)
                self.assertEqual(len(router.requests), 1)
                self.assertFalse((agent.workspace / 'later.txt').exists())
                ctx = agent.execution_context()
                self.addCleanup(ctx.session.close)
                receipt = ActionExecutor().query(ctx, report.steps[0].action_id)
                self.assertEqual(receipt['meta']['execution_state'], 'unknown')
                phases = [e['phase'] for e in report.events if e['type'] == 'workflow_state']
                self.assertEqual(phases[-1], 'stopped')

    def test_malformed_native_batches_fail_bounded_without_dispatch(self):
        for rows in ([{'name': 'write_file', 'args': {'path': 'bad', 'content': 'x'}}, None],
                     [{'id': 'same', 'name': 'write_file', 'args': {}},
                      {'id': 'same', 'name': 'write_file', 'args': {}}], 12):
            with self.subTest(rows=rows):
                agent, router = self.agent([native(rows)] * 3)
                report = agent.run('run malformed batch')
                self.assertEqual(report.stopped, 'format_error')
                self.assertEqual(len(router.requests), 3)
                self.assertFalse(any(e['type'] == 'tool_started' for e in report.events))
                self.assertFalse((agent.workspace / 'bad').exists())

    def test_malformed_function_shape_is_feedback_not_attribute_error(self):
        calls = toolwire.parse_tool_calls({'tool_calls': [{'id': 'bad', 'function': 'wrong'}]})
        agent, _ = self.agent([native([c.to_raw() for c in calls])] * 3)
        self.assertEqual(agent.run('inspect').stopped, 'format_error')

    def test_missing_ids_are_paired_and_provider_reasoning_preserved(self):
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire):
                call = toolwire.ToolCall('', 'list_dir', {}, wire)
                original = toolwire.assistant_message('', [call], wire)
                if wire == 'openai':
                    original['reasoning_content'] = 'provider reasoning'
                else:
                    original['content'].insert(0, {'type': 'thinking', 'thinking': 'reasoning', 'signature': 'signed'})
                snapshot = deepcopy(original)
                agent, router = self.agent([native([call.to_raw()], wire, original), 'done'])
                self.assertEqual(agent.run('list files').stopped, 'final')
                replay, result = router.requests[1][-2:]
                if wire == 'openai':
                    call_id = replay['tool_calls'][0]['id']
                    self.assertEqual(result['tool_call_id'], call_id)
                    self.assertEqual(replay['reasoning_content'], 'provider reasoning')
                else:
                    call_id = replay['content'][1]['id']
                    self.assertEqual(result['content'][0]['tool_use_id'], call_id)
                    self.assertEqual(replay['content'][0]['signature'], 'signed')
                self.assertTrue(call_id)
                self.assertEqual(original, snapshot)

    def test_native_non_object_arguments_rejected_without_loop_crash(self):
        agent, _ = self.agent([native([{'name': 'list_dir', 'args': 123}]), 'done'])
        report = agent.run('list files')
        self.assertEqual(report.steps[0].meta['error_code'], 'invalid_arguments')
        self.assertEqual(report.stopped, 'final')

    def test_nonfinite_text_arguments_produce_feedback_not_journal_crash(self):
        agent, _ = self.agent([text_call('list_dir', {'limit': float('nan')}), 'done'])
        report = agent.run('inspect')
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(report.steps[0].meta['error_code'], 'invalid_arguments')

    def test_bad_anthropic_input_is_normalized_for_valid_feedback_replay(self):
        message = {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'one', 'name': 'list_dir', 'input': ['wrong']}]}
        calls = toolwire.parse_tool_calls(message, 'anthropic')
        agent, router = self.agent([native([c.to_raw() for c in calls], 'anthropic', message), 'done'])
        report = agent.run('inspect')
        self.assertEqual(report.steps[0].meta['error_code'], 'invalid_arguments')
        self.assertIn('_raw', router.requests[1][-2]['content'][0]['input'])

    def test_failed_child_is_not_a_successful_tool_result(self):
        child_router = Router([TransportError('offline provider unavailable')])
        agent, _ = self.agent([text_call('spawn_subagent', {'task': 'inspect'}), 'child failed'],
                              child_router_factory=lambda *a: child_router)
        report = agent.run('delegate inspection')
        self.assertEqual(report.steps[0].decision, 'error')
        self.assertEqual(report.steps[0].meta['child_outcome'], 'failed')
        self.assertEqual(report.steps[0].meta['execution_state'], 'completed')
        self.assertIn('offline provider unavailable', report.steps[0].result)

    def test_unknown_child_stops_parent_and_preserves_child_identity(self):
        registry = build_builtin_registry()
        invoked = []
        def partial(a, ctx):
            invoked.append(1)
            (ctx.workspace / 'child.txt').write_text('partial')
            raise OSError('child interrupted after write')
        registry.register(ToolSpec('partial_write', '', partial, read_only=False), trusted=True)
        child_router = Router([text_call('partial_write', {})])
        agent, parent_router = self.agent([
            text_call('spawn_subagent', {'task': 'write', 'mode': Mode.ACCEPT_EDITS.value}), 'incorrect success'],
            registry, child_router_factory=lambda *a: child_router)
        report = agent.run('delegate write')
        self.assertEqual(report.stopped, 'unknown_outcome')
        self.assertEqual(len(parent_router.requests), 1)
        self.assertEqual(invoked, [1])
        self.assertEqual(report.steps[0].meta['child_outcome'], 'unknown')
        self.assertNotEqual(report.steps[0].meta['child_run_id'], report.run_id)

    def test_child_inherits_anthropic_wire_declarations(self):
        child_router = Router(['done'])
        agent, _ = self.agent([text_call('spawn_subagent', {'task': 'inspect'}), 'done'],
                              wire_hint='anthropic', child_router_factory=lambda *a: child_router)
        observed = []
        complete = child_router.complete
        def capture(messages, **kwargs):
            observed.extend(kwargs['tools'])
            return complete(messages, **kwargs)
        child_router.complete = capture
        self.assertEqual(agent.run('delegate').steps[0].decision, 'ok')
        self.assertTrue(observed)
        self.assertTrue(all('input_schema' in tool for tool in observed))

    def test_child_receipt_loss_stops_parent_after_real_write(self):
        registry = build_builtin_registry()
        def write(a, ctx):
            (ctx.workspace / 'child.txt').write_text('written')
            return ToolResult(True, 'done')
        registry.register(ToolSpec('child_write', '', write, read_only=False), trusted=True)
        child_router = Router([text_call('child_write', {})])
        agent, router = self.agent([text_call('spawn_subagent', {'task': 'write'}), 'incorrect success'],
            registry, child_router_factory=lambda *a: child_router)
        append = Session.append
        def broken_receipt(session, kind, **data):
            if kind == 'tool_completed' and data.get('tool') == 'child_write':
                raise OSError('completion receipt unavailable')
            return append(session, kind, **data)
        with patch.object(Session, 'append', broken_receipt):
            report = agent.run('delegate write')
        self.assertEqual(report.stopped, 'unknown_outcome')
        self.assertEqual(len(router.requests), 1)
        self.assertEqual((agent.workspace / 'child.txt').read_text(), 'written')
        self.assertEqual(report.steps[0].meta['error_code'], 'subagent_error')
        self.assertTrue(report.steps[0].meta['child_run_id'])

    def test_agent_edit_diff_real_test_and_checkpoint_restore(self):
        agent, _ = self.agent([text_call('read_file', {'path': 'value.py'}),
                              text_call('write_file', {'path': 'value.py', 'content': 'VALUE = 2\n'}), 'done'])
        path = agent.workspace / 'value.py'
        path.write_text('VALUE = 1\n')
        agent.checkpoints = CheckpointStore(agent.home, agent.workspace)
        if not agent.checkpoints.enabled:
            self.skipTest('Git unavailable')
        agent.file_guard = FileRevisionGuard(agent.workspace)
        report = agent.run('Update VALUE to 2')
        self.assertEqual(report.as_result().outcome, 'succeeded')
        write = report.steps[1]
        self.assertIn('+VALUE = 2', write.meta['file_changes'][0]['diff'])
        test = subprocess.run([sys.executable, '-B', '-c',
            'import unittest; from value import VALUE; '
            'case = unittest.FunctionTestCase(lambda: unittest.TestCase().assertEqual(VALUE, 2)); '
            'result = unittest.TextTestRunner().run(case); raise SystemExit(not result.wasSuccessful())'],
            cwd=agent.workspace, capture_output=True, text=True, timeout=20)
        self.assertEqual(test.returncode, 0, test.stderr)
        self.assertTrue(agent.checkpoints.rollback(write.meta['checkpoint']))
        self.assertEqual(path.read_text(), 'VALUE = 1\n')

    def test_failed_git_staging_or_commit_never_reuses_old_checkpoint(self):
        for phase in ('add', 'commit'):
            with self.subTest(phase=phase):
                agent, _ = self.agent([text_call('write_file', {'path': 'value.txt', 'content': 'new'}), 'done'])
                path = agent.workspace / 'value.txt'
                path.write_text('old')
                store = agent.checkpoints = CheckpointStore(agent.home, agent.workspace)
                if not store.enabled:
                    self.skipTest('Git unavailable')
                initial = store.snapshot('initial')
                run = subprocess.run
                def fail_command(argv, **kwargs):
                    if argv[0] == 'git' and phase in argv:
                        # A real nonzero Git exit; honour the caller's check flag.
                        argv = [*argv[:3], 'forge-nonexistent-command']
                    return run(argv, **kwargs)
                with patch('forge.checkpoint.subprocess.run', side_effect=fail_command):
                    report = agent.run('update file')
                self.assertEqual(report.steps[0].meta['execution_state'], 'not_started')
                self.assertEqual(report.steps[0].meta['error_code'], 'preparation_failed')
                self.assertNotIn('checkpoint', report.steps[0].meta)
                self.assertEqual(path.read_text(), 'old')
                self.assertEqual(store.history()[-1].commit, initial.commit)

    def test_checkpoint_timeout_is_recorded_without_dispatch(self):
        agent, _ = self.agent([text_call('write_file', {'path': 'bad.txt', 'content': 'bad'}), 'done'])
        store = agent.checkpoints = CheckpointStore(agent.home, agent.workspace)
        if not store.enabled:
            self.skipTest('Git unavailable')
        with patch('forge.checkpoint.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 30)) as run:
            report = agent.run('update file')
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
        self.assertEqual(report.steps[0].meta['error_code'], 'preparation_failed')
        self.assertEqual(report.steps[0].meta['execution_state'], 'not_started')
        self.assertFalse((agent.workspace / 'bad.txt').exists())


if __name__ == '__main__':
    unittest.main()
