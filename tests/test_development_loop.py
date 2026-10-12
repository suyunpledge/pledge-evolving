"""Real runtime/editor evidence contracts; no network or real user homes."""
import tempfile
import unittest
from pathlib import Path

from forge.development.editor import Document, ProjectBoundary, ConflictError
from forge.development.evidence import TaskEvidence
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.loop import Agent, LoopLimits
from forge.policy import Policy, Mode, Sandbox
from forge.tools import build_builtin_registry
from forge.test_task_planning import Router


class DevelopmentLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.home = self.root / 'home'
        self.path = self.project / 'x.py'
        self.path.write_text('a = 1\nb = 2\nc = 3\n', encoding='utf-8')

    def document(self):
        return Document.open(ProjectBoundary(self.project), self.path)

    def test_clean_buffer_reconciles_real_disk_change(self):
        d = self.document()
        self.path.write_text('agent changed\n')
        disk = d.inspect_external()
        self.assertEqual(d.reconcile(disk), 'reloaded')
        self.assertEqual(d.text, 'agent changed\n')
        self.assertFalse(d.dirty)

    def test_user_typing_during_background_read_is_preserved(self):
        d = self.document()
        self.path.write_text('agent changed\n')
        disk = d.inspect_external()
        d.update('user unsaved\n')
        self.assertEqual(d.reconcile(disk), 'conflict')
        self.assertEqual(d.text, 'user unsaved\n')
        self.assertIn('+agent changed', d.external_diff())

    def test_three_way_merge_only_non_overlapping_edits(self):
        d = self.document()
        d.update('a = 10\nb = 2\nc = 3\n')
        self.path.write_text('a = 1\nb = 2\nc = 30\n')
        d.reconcile(d.inspect_external())
        d.merge_external()
        self.assertEqual(d.text, 'a = 10\nb = 2\nc = 30\n')
        self.assertTrue(d.dirty)
        d.save()
        self.assertEqual(self.path.read_text(), d.text)
        d.update('a = 100\nb = 2\nc = 30\n')
        self.path.write_text('a = 99\nb = 2\nc = 30\n')
        d.reconcile(d.inspect_external())
        with self.assertRaises(ConflictError):
            d.merge_external()
        self.assertIn('100', d.text)
        self.assertIn('99', self.path.read_text())

    def test_deleted_or_binary_external_version_never_clears_buffer(self):
        d = self.document()
        self.path.unlink()
        self.assertEqual(d.reconcile(d.inspect_external()), 'conflict')
        self.assertIn('a = 1', d.text)
        self.path.write_bytes(b'\0binary')
        self.assertEqual(d.reconcile(d.inspect_external()), 'conflict')
        with self.assertRaises(ConflictError):
            d.merge_external()

    def test_evidence_never_marks_plan_or_tests_complete_from_prose(self):
        p = TaskEvidence()
        p.apply({'type': 'task_plan', 'run_id': 'r', 'plan': {'tasks': ['Read', 'Edit', 'Test']}})
        p.apply({'type': 'assistant_message', 'content': 'all tests passed, 100%'})
        self.assertEqual(p.plan, ['Read', 'Edit', 'Test'])
        self.assertEqual(p.tests, [])
        self.assertEqual(p.actions, {})
        p.apply({'type': 'tool_started', 'run_id': 'r', 'action_id': 'a', 'tool': 'shell_exec'})
        p.apply({'type': 'tool_completed', 'run_id': 'r', 'action_id': 'a', 'tool': 'shell_exec', 'ok': False})
        p.apply({'type': 'tool_call', 'tool': 'shell_exec', 'ok': False, 'authorization': 'deny', 'result': 'Denied'})
        self.assertEqual(p.actions['a']['status'], 'failed')
        self.assertEqual(p.tests[-1]['status'], 'blocked')
        self.assertIsNone(p.tests[-1]['passed'])

    def test_subagent_evidence_scoped_by_real_agent_and_run(self):
        p = TaskEvidence()
        p.apply({'type': 'workflow_state', 'run_id': 'parent', 'agent': 'forge', 'phase': 'executing'})
        p.apply({'type': 'workflow_state', 'run_id': 'child', 'agent': 'forge/sub1', 'depth': 1, 'phase': 'executing'})
        p.apply({'type': 'workflow_state', 'run_id': 'child', 'agent': 'forge/sub1', 'depth': 1, 'phase': 'completed'})
        self.assertEqual(p.agents['forge']['status'], 'executing')
        self.assertEqual(p.agents['forge/sub1']['status'], 'completed')

    def test_optional_child_router_and_observer_use_existing_spawn(self):
        parent = Router(['parent done'])
        child = Router(['child done'])
        events = []
        agent = Agent(home=self.home, workspace=self.project, router=parent,
                      registry=build_builtin_registry(), policy=Policy(mode=Mode.DONT_ASK, sandbox=Sandbox.READ_ONLY),
                      limits=LoopLimits(max_depth=2, spawn_budget=3),
                      child_router_factory=lambda name, policy, inherited: child,
                      child_observer=events.append)
        self.addCleanup(agent.secret_scope.close)
        result = agent._spawn_handler('inspect real project', 'plan')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(child.calls), 1)
        self.assertEqual(parent.calls, [])
        self.assertIs(agent.router, parent)
        self.assertTrue(any(e['type'] == 'workflow_state' and e['agent'].endswith('/sub1') for e in events))

    def test_child_router_failure_does_not_fallback_or_consume_budget(self):
        def broken(*args):
            raise ValueError('invalid child model')
        agent = Agent(home=self.home, workspace=self.project, router=Router(['must not run']),
                      registry=build_builtin_registry(), policy=Policy(mode=Mode.DONT_ASK, sandbox=Sandbox.READ_ONLY),
                      child_router_factory=broken)
        self.addCleanup(agent.secret_scope.close)
        budget = agent.budget[0]
        result = agent._spawn_handler('task', 'plan')
        self.assertFalse(result.ok)
        self.assertEqual(agent.budget[0], budget)

    def test_optional_hooks_preserve_depth_budget_and_observer_fault_isolation(self):
        calls = []
        def observer(event):
            raise RuntimeError('observer failure')
        agent = Agent(home=self.home, workspace=self.project, router=Router(['done']),
                      registry=build_builtin_registry(), policy=Policy(mode=Mode.DONT_ASK, sandbox=Sandbox.READ_ONLY),
                      limits=LoopLimits(max_depth=0), child_router_factory=lambda *args: calls.append(args),
                      child_observer=observer)
        self.addCleanup(agent.secret_scope.close)
        self.assertFalse(agent._spawn_handler('task', 'plan').ok)
        self.assertEqual(calls, [])
        agent.limits = LoopLimits(max_depth=2)
        agent.budget[0] = 0
        self.assertFalse(agent._spawn_handler('task', 'plan').ok)
        self.assertEqual(calls, [])
        agent.budget[0] = 1
        agent.child_router_factory = lambda *args: Router(['actual child reply'])
        self.assertTrue(agent._spawn_handler('task', 'plan').ok)

    def test_agent_models_persist_and_task_snapshot_is_frozen(self):
        state = ModeState(self.home)
        state.configure_agent_model('forge/sub1', ('first', 'model'))
        snapshot = state.task_snapshot()
        state.configure_agent_model('forge/sub1', ('second', 'model'))
        self.assertEqual(dict(snapshot.agent_models)['forge/sub1'], ('first', 'model'))
        self.assertEqual(ModeState(self.home).value('agent_models')['forge/sub1'], ['second', 'model'])

    def test_resume_links_old_uncertain_journal_without_replaying_action(self):
        from forge.session import Session
        rt = self.runtime()
        old = rt.home / 'sessions' / 'old.jsonl'
        with Session(old, meta={'cwd': str(self.project)}) as journal:
            journal.append('user_message', content='old request')
            journal.append('workflow_state', run_id='interrupted', phase='executing')
            journal.append('tool_started', run_id='interrupted', action_id='unknown-write', tool='write_file')
        original = old.read_bytes()
        rt.restore_history(old)
        self.assertEqual(rt.evidence.actions['unknown-write']['status'], 'unknown')
        self.assertEqual(rt.evidence.phase, 'interrupted')
        rt.bridge.router = Router(['Inspected; no action replayed'])
        result = rt.run('continue by inspecting', planning='none')
        self.assertTrue(result['ok'])
        self.assertFalse(rt.evidence.actions)
        self.assertEqual(old.read_bytes(), original)
        events = list(rt.bridge.agent.session.replay())
        links = [e for e in events if e.type == 'development_resume']
        self.assertEqual(links[0].data['workflow']['pending_actions'], ['unknown-write'])

    def test_recovery_ignores_child_dialogue_and_keeps_torn_tail_as_evidence(self):
        from forge.session import Session
        path = self.home / 'sessions' / 'old.jsonl'
        with Session(path, meta={'cwd': str(self.project)}) as s:
            s.append('user_message', agent='forge', depth=0, content='parent task')
            s.append('user_message', agent='forge/sub1', depth=1, content='child task')
            s.append('assistant_message', agent='forge/sub1', depth=1, content='child reply')
            s.append('workflow_state', agent='forge', depth=0, run_id='root', phase='executing')
            s.append('tool_started', agent='forge', depth=0, run_id='root', action_id='unknown', tool='write_file')
        with path.open('ab') as f:
            f.write(b'{"unfinished":')
        before = path.read_bytes()
        rt = DevelopmentRuntime(ModeState(self.home))
        self.addCleanup(rt.close)
        evidence = rt.recovery(path)
        self.assertEqual(evidence['history'], [{'role': 'user', 'content': 'parent task'}])
        self.assertTrue(evidence['incomplete_tail'])
        self.assertEqual(evidence['workflow']['pending_actions'], ['unknown'])
        self.assertEqual(path.read_bytes(), before)

    def runtime(self):
        rt = DevelopmentRuntime(ModeState(self.home))
        self.addCleanup(rt.close)
        rt.initialize(self.project)
        rt.state.switch('development')
        rt.bridge.agent.thinking_mode = 'off'
        rt.bridge.agent.thinking = False
        return rt

    def test_actual_parent_child_parent_routing_and_agent_override(self):
        from forge.model import Provider, ModelRouter
        from forge.test_phase_models import Transport
        rt = self.runtime()
        reply = '<tool_call>{"tool":"spawn_subagent","args":{"task":"read x.py", "mode":"plan"}}</tool_call>'
        transport = Transport([reply, 'child read result', 'parent done'])
        providers = [Provider('writer', 'https://writer.invalid', default_model='code'),
                     Provider('helper', 'https://helper.invalid', default_model='small'),
                     Provider('own', 'https://own.invalid', default_model='independent')]
        router = ModelRouter(providers, transport=transport, primary=('writer', 'code'), retries_per_provider=0)
        rt.bridge.router = rt.bridge.agent.phase_catalog = router
        rt.bridge.models = {p.name: (p.name, p.default_model) for p in providers}
        rt.catalog = dict(rt.bridge.models)
        rt.state.configure_role('subagent', ('helper', 'small'))
        rt.state.configure_agent_model('forge/sub1', ('own', 'independent'))
        result = rt.run('inspect', permission='workspace-write', explicit_model=('writer', 'code'), planning='none')
        self.assertTrue(result['ok'], result)
        self.assertEqual([(p, m) for p, m, _, _ in transport.calls],
                         [('writer', 'code'), ('own', 'independent'), ('writer', 'code')])
        self.assertIn('forge/sub1', rt.evidence.agents)
        self.assertEqual(rt.evidence.agents['forge/sub1']['status'], 'completed')
        # Existing public report counts the parent; existing ledger folds child
        # tokens once. Preserve that contract instead of changing billing here.
        self.assertEqual(result['usage']['prompt_tokens'], 20)
        self.assertEqual(result['usage']['completion_tokens'], 10)
        self.assertEqual(sum(e['tokens'] for e in rt.bridge.agent.events if e['type'] == 'ledger_recorded'), 45)

    def test_checkpoint_restore_obeys_policy_and_revision(self):
        rt = self.runtime()
        point = rt.bridge.agent.checkpoints.snapshot('initial')
        self.assertIsNotNone(point)
        self.path.write_text("print('checkpoint-current-code')\n")
        preview = rt.checkpoint_preview(point.commit, self.path)
        self.assertNotIn('content', preview)
        denied = rt.restore_checkpoint(point.commit, self.path, preview['expected'])
        self.assertFalse(denied['ok'])
        self.assertEqual(self.path.read_text(), "print('checkpoint-current-code')\n")
        allowed = rt.restore_checkpoint(point.commit, self.path, preview['expected'], permission='workspace-write')
        self.assertTrue(allowed['ok'], allowed)
        self.assertIn('a = 1', self.path.read_text())
        with self.assertRaises(ValueError):
            rt.restore_checkpoint(point.commit, self.path, preview['expected'], permission='workspace-write')

    def test_crlf_code_is_writable_but_crlf_secret_config_stays_protected(self):
        rt = self.runtime()
        self.path.write_bytes(b'print(1)\r\n')
        agent = rt.bridge.agent
        agent.policy = rt.bridge.policy('workspace-write')
        result = agent.registry.invoke('write_file', {'path': str(self.path), 'content': 'print(2)\n'}, agent._tool_context())
        self.assertTrue(result.ok, result.error)
        config = self.project / 'keys.env'
        config.write_bytes(b'API_KEY=sk-crlf-synthetic-123456789\r\n')
        result = agent.registry.invoke('write_file', {'path': str(config), 'content': 'API_KEY=changed\n'}, agent._tool_context())
        self.assertFalse(result.ok)
        self.assertIn(b'sk-crlf-synthetic', config.read_bytes())

    def test_checkpoint_restore_never_exports_or_rewrites_secret_values(self):
        rt = self.runtime()
        config = self.project / '.env'
        secret = 'sk-checkpoint-synthetic-123456789'
        config.write_text('API_KEY=' + secret)
        point = rt.bridge.agent.checkpoints.snapshot('secret config')
        config.write_text('API_KEY=changed')
        preview = rt.checkpoint_preview(point.commit, config)
        self.assertNotIn(secret, str(preview))
        result = rt.restore_checkpoint(point.commit, config, preview['expected'], permission='workspace-write')
        self.assertFalse(result['ok'])
        self.assertNotIn(secret, str(result))
        self.assertEqual(config.read_text(), 'API_KEY=changed')

    def test_checkpoint_diff_protects_json_escaped_secret_field_names(self):
        rt = self.runtime()
        config = self.project / 'settings.json'
        old = 'legacy-internal-opaque-value-Q9'
        config.write_text('{"\\u0074oken":"' + old + '","model":"first"}', encoding='utf-8')
        point = rt.bridge.agent.checkpoints.snapshot('escaped credential')
        config.write_text('{"\\u0074oken":"new-internal-opaque-value-Z8","model":"second"}', encoding='utf-8')
        preview = rt.checkpoint_preview(point.commit, config)
        self.assertNotIn(old, str(preview))
        self.assertNotIn('new-internal-opaque-value-Z8', str(preview))
