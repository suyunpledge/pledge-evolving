"""Real Agent, Git, process and concurrent-runtime contracts; offline model I/O."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'forge-gui'))
from development_mode.project import WorkspaceSource
from forge.development.editor import Document, ProjectBoundary, ConflictError
from forge.development.evidence import TaskEvidence
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.model import Completion, Usage, ModelRouter, Provider
from forge.test_task_planning import Router
from forge.test_phase_models import Transport


def call(name, **args):
    return '<tool_call>' + json.dumps({'tool': name, 'args': args}) + '</tool_call>'


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.project / 'value.py'
        self.path.write_text('VALUE = 1\n', encoding='utf-8')
        network = patch('forge.model.HttpTransport.complete', side_effect=AssertionError('External model I/O forbidden in this suite'))
        network.start()
        self.addCleanup(network.stop)

    def runtime(self, name='home', router=None):
        rt = DevelopmentRuntime(ModeState(self.root / name))
        rt.initialize(self.project)
        rt.state.switch('development')
        rt.bridge.agent.thinking_mode = 'off'
        rt.bridge.agent.thinking = False
        rt.bridge.agent.planning_level = 'none'
        rt.bridge.agent.router = Router([AssertionError('Task must retain its actual router for review')])
        if router:
            rt.bridge.router = router
        self.addCleanup(rt.close)
        return rt

    def run_task(self, rt):
        return rt.run('scoped task', permission='workspace-write', planning='none')

    def test_true_execution_loop_and_recovery(self):
        for args in [('init', '-q'), ('config', 'user.name', 'Fixture'),
                     ('config', 'user.email', 'fixture@example.invalid'), ('add', '.'),
                     ('commit', '-qm', 'initial')]:
            subprocess.run(['git', *args], cwd=self.project, check=True, capture_output=True)
        tests = self.project / 'tests'
        tests.mkdir()
        (tests / 'test_value.py').write_text('import unittest\nfrom value import VALUE\n'
            'class ValueTest(unittest.TestCase):\n'
            '    def test_value(self): self.assertEqual(VALUE, 2)\n', encoding='utf-8')
        rt = self.runtime(router=Router([call('read_file', path='value.py'),
            call('write_file', path='value.py', content='VALUE = 2\n'), 'Edited value.py']))
        editor = Document.open(ProjectBoundary(self.project), self.path)
        point = rt.bridge.agent.checkpoints.snapshot('before acceptance')
        result = self.run_task(rt)
        self.assertTrue(result['ok'], result)
        self.assertEqual(editor.reconcile(editor.inspect_external()), 'reloaded')
        self.assertEqual(editor.text, 'VALUE = 2\n')
        diff = rt.inspect_diff(WorkspaceSource(self.project), self.path)
        self.assertIn('+VALUE = 2', diff)
        test = rt.execute_tests([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], trusted=True)
        self.assertEqual(test['exit_code'], 0, test)
        self.assertIn('Ran 1 test', test['output'])
        self.assertEqual(test['task_id'], result['task_id'])
        self.assertEqual(rt.evidence.tests[-1]['status'], 'completed')
        self.assertTrue(Path(test['artifact']).is_file())
        saved_test = json.loads(Path(test['artifact']).read_text(encoding='utf-8'))
        self.assertEqual(saved_test['task_id'], result['task_id'])
        self.assertEqual(saved_test['run_id'], test['run_id'])
        self.assertEqual(rt.evidence.diffs[-1]['task_id'], result['task_id'])
        preview = rt.checkpoint_preview(point.commit, self.path)
        restored = rt.restore_checkpoint(point.commit, self.path, preview['expected'], permission='workspace-write')
        self.assertTrue(restored['ok'], restored)
        self.assertEqual(self.path.read_text(), 'VALUE = 1\n')
        journal = rt.bridge.agent.session.path
        recovery = rt.restore_history(journal)
        self.assertEqual(rt.evidence.task_id, result['task_id'])
        self.assertEqual(rt.evidence.tests[-1]['exit_code'], 0)
        self.assertIsNone(rt.last_snapshot)
        with self.assertRaises(PermissionError):
            rt.execute_tests([sys.executable, '-c', 'pass'], trusted=True)
        self.assertEqual(ModeState(rt.state.home).value('project'), str(self.project))
        if target := os.environ.get('FORGE_ACCEPTANCE_OUTPUT'):
            Path(target).write_text(json.dumps({'task_id': result['task_id'],
                'result': result, 'diff': diff, 'test': test, 'restore': restored,
                'recovered_events': len(recovery['events']),
                'final_content': self.path.read_text(), 'model_transport': 'offline deterministic fixture'},
                ensure_ascii=False, indent=2), encoding='utf-8')

    def test_models_main_child_review_and_frozen_review_choice(self):
        rt = self.runtime()
        transport = Transport([call('spawn_subagent', task='inspect', mode='plan'),
                               'child finished', 'parent finished', 'Review finding'])
        providers = [Provider(n, 'https://' + n + '.invalid', default_model=m)
                     for n, m in [('writer', 'code'), ('child', 'small'), ('reviewer', 'review')]]
        router = ModelRouter(providers, transport=transport, primary=('writer', 'code'), retries_per_provider=0)
        rt.bridge.router = rt.bridge.agent.phase_catalog = router
        rt.catalog = rt.bridge.models = {p.name: (p.name, p.default_model) for p in providers}
        rt.state.configure_role('default', ('writer', 'code'))
        rt.state.configure_role('subagent', ('child', 'small'))
        rt.state.configure_role('review', ('reviewer', 'review'))
        agent = rt.bridge.agent
        saved = (agent.policy, agent.router, agent.review_model, agent.child_observer, agent.extra_system)
        result = self.run_task(rt)
        rt.state.configure_role('review', ('writer', 'code'))
        review = rt.review()
        self.assertEqual(review['task_id'], result['task_id'])
        self.assertEqual(next(iter(rt.evidence.reviews.values()))['review']['model'], 'review')
        self.assertEqual([(p, m) for p, m, _, _ in transport.calls],
                         [('writer', 'code'), ('child', 'small'), ('writer', 'code'), ('reviewer', 'review')])
        self.assertEqual(saved, (agent.policy, agent.router, agent.review_model, agent.child_observer, agent.extra_system))

    def test_general_mode_ignores_invalid_development_models(self):
        rt = self.runtime(router=Router(['general finished']))
        rt.state.configure_role('subagent', ('missing', 'missing'))
        rt.state.configure_role('review', ('missing', 'missing'))
        rt.state.configure_agent_model('forge/sub1', ('missing', 'missing'))
        rt.state.switch('general')
        policy = rt.bridge.agent.policy
        result = rt.run('general', child_model=('missing', 'missing'))
        self.assertTrue(result['ok'])
        self.assertIs(rt.bridge.agent.policy, policy)
        self.assertIsNone(rt.bridge.agent.file_guard)
        with self.assertRaises(PermissionError):
            rt.execute_tests([sys.executable, '-c', 'pass'], trusted=True)

    def test_invalid_model_and_observer_do_not_pollute_next_task(self):
        rt = self.runtime(router=Router(['done']))
        agent = rt.bridge.agent
        original = agent.policy, agent.router, agent.extra_system
        with self.assertRaises(ValueError):
            rt.run('bad', explicit_model=('missing', 'model'))
        bad = rt.last_task_id
        self.assertEqual(rt.tasks[bad]['evidence'].phase, 'failed')
        rt.result_observers.append(lambda _: (_ for _ in ()).throw(ValueError('observer failed')))
        result = self.run_task(rt)
        self.assertTrue(result['ok'])
        self.assertEqual(rt.evidence.phase, 'completed')
        self.assertEqual(original, (agent.policy, agent.router, agent.extra_system))
        self.assertNotEqual(bad, result['task_id'])

    def test_cancellation_after_model_returns_prevents_tool_side_effect(self):
        entered, release = threading.Event(), threading.Event()
        class Blocking(Router):
            def complete(inner, messages, **kw):
                entered.set()
                if not release.wait(10): raise RuntimeError('fixture timeout')
                return Completion(call('write_file', path='unexpected.py', content='bad'), Usage(10, 5))
        rt = self.runtime(router=Blocking([]))
        results = []
        worker = threading.Thread(target=lambda: results.append(self.run_task(rt)))
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(10))
        self.assertTrue(rt.cancel())
        self.assertTrue(rt.busy)
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['stopped'], 'cancelled')
        self.assertEqual(results[0]['usage']['prompt_tokens'], 10)
        self.assertEqual(rt.evidence.phase, 'cancelled')
        self.assertFalse((self.project / 'unexpected.py').exists())
        self.assertFalse(rt.busy)
        rt.bridge.router = Router(['next completed'])
        self.assertTrue(self.run_task(rt)['ok'])

    def test_child_cancellation_propagates_to_parent(self):
        rt = self.runtime()
        entered, release = threading.Event(), threading.Event()
        class TransportWithBlock(Transport):
            def complete(inner, provider, model, messages, **kw):
                if provider.name == 'child':
                    entered.set()
                    if not release.wait(10): raise RuntimeError('fixture timeout')
                return super().complete(provider, model, messages, **kw)
        transport = TransportWithBlock([call('spawn_subagent', task='work', mode='acceptEdits'),
                                      call('write_file', path='unexpected.py', content='bad')])
        providers = [Provider('writer', 'https://writer.invalid', default_model='code'),
                     Provider('child', 'https://child.invalid', default_model='small')]
        router = ModelRouter(providers, transport=transport, primary=('writer', 'code'), retries_per_provider=0)
        rt.bridge.router = rt.bridge.agent.phase_catalog = router
        rt.catalog = rt.bridge.models = {p.name: (p.name, p.default_model) for p in providers}
        rt.state.configure_role('subagent', ('child', 'small'))
        results = []
        worker = threading.Thread(target=lambda: results.append(self.run_task(rt)))
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(10))
        rt.cancel()
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['stopped'], 'cancelled')
        self.assertEqual(rt.evidence.agents['forge/sub1']['status'], 'cancelled')
        self.assertFalse((self.project / 'unexpected.py').exists())

    def test_two_simultaneous_agents_conflict_without_losing_a_write(self):
        barrier = threading.Barrier(2)
        class Concurrent(Router):
            def complete(inner, messages, **kw):
                if len(inner.calls) == 1: barrier.wait(timeout=10)
                return super().complete(messages, **kw)
        runtimes = [self.runtime('home-' + str(i), Concurrent([call('read_file', path='value.py'),
            call('write_file', path='value.py', content=f'VALUE = {i + 2}\n'), 'finished'])) for i in range(2)]
        results, errors = [], []
        def run(rt):
            try: results.append(self.run_task(rt))
            except Exception as exc: errors.append(exc)
        workers = [threading.Thread(target=run, args=(rt,)) for rt in runtimes]
        for w in workers: w.start()
        for w in workers: w.join(15)
        self.assertFalse(any(w.is_alive() for w in workers))
        self.assertFalse(errors, errors)
        self.assertEqual(len({r['task_id'] for r in results}), 2)
        writes = [next(r for r in rt.evidence.actions.values() if r['tool'] == 'write_file') for rt in runtimes]
        self.assertEqual(sorted(r['ok'] for r in writes), [False, True])
        loser = next(r for r in writes if not r['ok'])
        self.assertIn('changed since preview', loser['result'])
        self.assertIn(self.path.read_text(), ['VALUE = 2\n', 'VALUE = 3\n'])
        for rt in runtimes:
            self.assertTrue(all(r['task_id'] == rt.last_task_id for r in rt.evidence.actions.values()))
            self.assertEqual(len(rt.evidence.actions), 2)

    def test_agent_write_preserves_dirty_editor_and_conflict_can_merge(self):
        editor = Document.open(ProjectBoundary(self.project), self.path)
        editor.update('VALUE = 99\n')
        rt = self.runtime(router=Router([call('read_file', path='value.py'),
            call('write_file', path='value.py', content='VALUE = 2\n'), 'done']))
        self.run_task(rt)
        self.assertEqual(editor.reconcile(editor.inspect_external()), 'conflict')
        self.assertEqual(editor.text, 'VALUE = 99\n')
        with self.assertRaises(ConflictError): editor.save()
        with self.assertRaises(ConflictError): editor.merge_external()
        editor.reload()
        self.assertEqual(editor.text, 'VALUE = 2\n')

    def test_read_before_write_guard_and_secret_shell_boundary(self):
        rt = self.runtime(router=Router([call('write_file', path='value.py', content='overwrite'),
            call('shell_exec', command='echo unsafe'), 'done']))
        self.run_task(rt)
        rows = list(rt.evidence.actions.values())
        self.assertIn('Read the existing file', rows[0]['result'])
        self.assertEqual(rows[1]['authorization'], 'deny')
        self.assertEqual(rows[1]['meta']['boundary'], 'secret.isolation')
        self.assertEqual(self.path.read_text(), 'VALUE = 1\n')

    def test_native_tests_fail_timeout_cancel_and_policy(self):
        rt = self.runtime(router=Router(['done']))
        self.run_task(rt)
        with self.assertRaises(PermissionError): rt.execute_tests([sys.executable, '-c', 'pass'])
        failure = rt.execute_tests([sys.executable, '-c', 'raise SystemExit(7)'], trusted=True)
        self.assertEqual((failure['status'], failure['exit_code']), ('failed', 7))
        timeout = rt.execute_tests([sys.executable, '-c', 'import time; time.sleep(30)'], trusted=True, timeout=.1)
        self.assertEqual(timeout['status'], 'timed_out')
        seen = threading.Event()
        previous_emit = rt.emit
        rt.emit = lambda e: (seen.set() if e.get('data', {}).get('type') == 'test_started' else None, previous_emit(e))
        results = []
        worker = threading.Thread(target=lambda: results.append(rt.execute_tests(
            [sys.executable, '-c', 'import time; time.sleep(30)'], trusted=True)))
        worker.start()
        self.assertTrue(seen.wait(10))
        self.assertTrue(rt.cancel())
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'cancelled')
        self.assertFalse(rt.busy)

    def test_action_details_join_by_id_and_recovery_limits_to_latest_task(self):
        events = []
        for task, run, action in [('old', 'r1', 'a1'), ('new', 'r2', 'a2')]:
            events += [{'type': 'development_task_started', 'task_id': task, 'phase': 'executing'},
                {'type': 'tool_started', 'task_id': task, 'run_id': run, 'action_id': action, 'tool': 'read_file'},
                {'type': 'workflow_state', 'task_id': task, 'run_id': run, 'phase': 'executing'}]
        view = TaskEvidence.from_events(events, recovered=True)
        self.assertEqual(set(view.actions), {'a2'})
        self.assertEqual(view.actions['a2']['status'], 'unknown')
        self.assertEqual(view.runs[('forge', 'r2')]['status'], 'interrupted')
        p = TaskEvidence()
        for action in ['one', 'two']:
            p.apply({'type': 'tool_started', 'action_id': action, 'run_id': action, 'tool': 'read_file'})
        p.apply({'type': 'tool_call', 'action_id': 'one', 'run_id': 'one', 'tool': 'read_file', 'result': 'first'})
        self.assertEqual(p.actions['one']['result'], 'first')
        self.assertNotIn('result', p.actions['two'])

    def test_parent_observation_is_stale_after_child_writes(self):
        rt = self.runtime()
        transport = Transport([call('read_file', path='value.py'),
            call('spawn_subagent', task='modify', mode='acceptEdits'),
            call('read_file', path='value.py'), call('write_file', path='value.py', content='VALUE = 2\n'),
            'child done', call('write_file', path='value.py', content='VALUE = 3\n'), 'parent done'])
        providers = [Provider('writer', 'https://writer.invalid', default_model='code'),
                     Provider('child', 'https://child.invalid', default_model='small')]
        router = ModelRouter(providers, transport=transport, primary=('writer', 'code'), retries_per_provider=0)
        rt.bridge.router = rt.bridge.agent.phase_catalog = router
        rt.catalog = rt.bridge.models = {p.name: (p.name, p.default_model) for p in providers}
        rt.state.configure_role('subagent', ('child', 'small'))
        self.run_task(rt)
        writes = [r for r in rt.evidence.actions.values() if r['tool'] == 'write_file']
        self.assertEqual([(r['agent'], r['ok']) for r in writes], [('forge/sub1', True), ('forge', False)])
        self.assertEqual(self.path.read_text(), 'VALUE = 2\n')

    def test_external_change_during_read_never_becomes_a_valid_observation(self):
        rt = self.runtime(router=Router([call('read_file', path='value.py'),
            call('write_file', path='value.py', content='VALUE = 9\n'), 'done']))
        registry = rt.bridge.agent.registry
        original = registry._specs['read_file'].handler
        def changing(args, ctx):
            result = original(args, ctx)
            self.path.write_text('VALUE = 7\n')
            return result
        with patch.object(registry._specs['read_file'], 'handler', changing):
            self.run_task(rt)
        rows = list(rt.evidence.actions.values())
        self.assertIn('changed during read', rows[0]['result'])
        self.assertFalse(rows[1]['ok'])
        self.assertEqual(self.path.read_text(), 'VALUE = 7\n')

    def test_review_cancellation_is_distinct_from_a_completed_review(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingReview(Router):
            def complete(inner, messages, **kw):
                if inner.calls:
                    entered.set()
                    if not release.wait(10): raise RuntimeError('fixture timeout')
                return super().complete(messages, **kw)
        rt = self.runtime(router=BlockingReview(['done', 'late review']))
        self.run_task(rt)
        results = []
        worker = threading.Thread(target=lambda: results.append(rt.review()))
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(10))
        self.assertTrue(rt.cancel())
        release.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['status'], 'cancelled')
        self.assertFalse(results[0]['ok'])
        self.assertEqual(next(iter(rt.evidence.reviews.values()))['status'], 'cancelled')
        self.assertFalse(rt.bridge.agent.review_enabled)
        self.assertFalse(rt.busy)
        self.assertTrue(any(e['type'] == 'review_cancelled' for e in rt.bridge.agent.events))

    def test_restoring_current_journal_preserves_original_and_saves_real_history(self):
        rt = self.runtime(router=Router(['first done', 'continued']))
        self.run_task(rt)
        original = rt.bridge.agent.session.path
        raw = original.read_bytes()
        recovered = rt.restore_history(original)
        fresh = rt.bridge.agent.session.path
        self.assertNotEqual(fresh, original)
        self.assertTrue(fresh.is_file())
        self.assertEqual(rt.recovery(fresh)['history'], recovered['history'])
        self.run_task(rt)
        self.assertEqual(original.read_bytes(), raw)
        self.assertEqual(len(rt.recovery(fresh)['history']), 4)

    def test_two_agents_on_different_files_keep_state_and_writes(self):
        (self.project / 'other.py').write_text('VALUE = 1\n')
        barrier = threading.Barrier(2)
        class Concurrent(Router):
            def complete(inner, messages, **kw):
                if len(inner.calls) == 1: barrier.wait(timeout=10)
                return super().complete(messages, **kw)
        runtimes = [self.runtime('independent-' + str(i), Concurrent([call('read_file', path=p),
            call('write_file', path=p, content='VALUE = 4\n'), 'done']))
            for i, p in enumerate(['value.py', 'other.py'])]
        errors = []
        def run(rt):
            try: self.run_task(rt)
            except Exception as exc: errors.append(exc)
        workers = [threading.Thread(target=run, args=(rt,)) for rt in runtimes]
        for w in workers: w.start()
        for w in workers: w.join(15)
        self.assertFalse(any(w.is_alive() for w in workers))
        self.assertFalse(errors, errors)
        for rt in runtimes:
            self.assertTrue(all(r['ok'] for r in rt.evidence.actions.values()))
        self.assertEqual(self.path.read_text(), 'VALUE = 4\n')
        self.assertEqual((self.project / 'other.py').read_text(), 'VALUE = 4\n')

    def test_test_output_redacted_and_explicit_deny_preserved(self):
        from dataclasses import replace
        rt = self.runtime(router=Router(['done']))
        self.run_task(rt)
        rt.bridge.base_policy = replace(rt.bridge.base_policy, deny=('shell_exec',))
        with self.assertRaises(PermissionError):
            rt.execute_tests([sys.executable, '-c', 'pass'], trusted=True)
        rt.bridge.base_policy = replace(rt.bridge.base_policy, deny=())
        secret = 'sk-synthetic-execution-output-123456789abcdef'
        result = rt.execute_tests([sys.executable, '-c', f'print("API_KEY={secret}")'], trusted=True)
        self.assertEqual(result['exit_code'], 0)
        self.assertNotIn(secret, result['output'])
        self.assertNotIn(secret, Path(result['artifact']).read_text(encoding='utf-8'))

    def test_recovery_public_session_identity_does_not_pollute_model_names(self):
        from forge.session import Session
        from forge.secrets import redact
        rt = self.runtime()
        identifier = 'public-rollout-fixture-identity'
        journal = rt.home / 'sessions' / (identifier + '.jsonl')
        with Session(journal, meta={'cwd': str(self.project)}) as log:
            log.append('user_message', content='real dialogue')
        recovery = rt.recovery(journal)
        self.assertEqual(recovery['events'][0]['session_id'], identifier)
        TaskEvidence.from_events(recovery['events'])
        self.assertEqual(redact(identifier), identifier)
        rt.state.configure_role('review', ('fixture', identifier))


if __name__ == '__main__':
    unittest.main()
