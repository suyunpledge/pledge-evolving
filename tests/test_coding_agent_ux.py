"""Real runtime/files/Git with offline transport; truthful UI projection contracts."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from forge.development.evidence import TaskEvidence
from forge.development.guard import FileRevisionGuard
from forge.development.runtime import DevelopmentRuntime
from forge.development.state import ModeState
from forge.test_task_planning import Router


def call(name, **args):
    return json.dumps({'tool': name, 'args': args})


class EvidenceTests(unittest.TestCase):
    def test_denied_intent_is_not_a_running_tool_and_reader_approval_is_not_a_patch(self):
        e = TaskEvidence()
        e.apply({'type': 'tool_started', 'action_id': 'a', 'tool': 'read_file', 'execution_state': 'not_started'})
        self.assertEqual(e.actions['a']['status'], 'not_started')
        e.apply({'type': 'tool_completed', 'action_id': 'a', 'tool': 'read_file', 'ok': False,
            'args': {'path': 'one.py'}, 'meta': {'requires_approval': True}})
        self.assertEqual(e.actions['a']['status'], 'awaiting_approval')
        self.assertFalse(e.changes)

    def test_run_updates_keep_actual_model_parent_and_task(self):
        e = TaskEvidence()
        e.apply({'type': 'development_agent_config', 'task_id': 't', 'agent': 'forge/sub1',
                 'selected_model': ['p', 'm'], 'model_source': 'independent', 'permission': 'read-only'})
        e.apply({'type': 'user_message', 'task_id': 't', 'agent': 'forge/sub1', 'run_id': 'child',
                 'parent_run_id': 'parent', 'depth': 1, 'content': 'inspect real source'})
        e.apply({'type': 'workflow_state', 'task_id': 't', 'agent': 'forge/sub1', 'run_id': 'child',
                 'parent_run_id': 'parent', 'depth': 1, 'phase': 'executing'})
        e.apply({'type': 'usage', 'task_id': 't', 'agent': 'forge/sub1', 'run_id': 'child', 'model': 'actual-model'})
        e.apply({'type': 'assistant_message', 'task_id': 't', 'agent': 'forge/sub1', 'run_id': 'child', 'content': 'done'})
        e.apply({'type': 'workflow_state', 'task_id': 't', 'agent': 'forge/sub1', 'run_id': 'child',
                 'parent_run_id': 'parent', 'depth': 1, 'phase': 'completed'})
        row = e.runs[('forge/sub1', 'child')]
        self.assertEqual((row['model'], row['parent_run_id'], row['task'], row['output']),
                         ('actual-model', 'parent', 'inspect real source', 'done'))
        self.assertEqual(row['model_source'], 'independent')

    def test_unknown_and_pending_never_become_written_or_completed(self):
        e = TaskEvidence()
        e.apply({'type': 'tool_completed', 'action_id': 'a', 'task_id': 't', 'tool': 'write_file',
                 'ok': False, 'execution_state': 'unknown', 'meta': {}})
        e.apply({'type': 'tool_completed', 'action_id': 'b', 'task_id': 't', 'tool': 'write_file',
                 'ok': False, 'args': {'path': 'one.py'}, 'meta': {'requires_approval': True}})
        self.assertEqual(e.actions['a']['status'], 'unknown')
        self.assertEqual(e.actions['b']['status'], 'awaiting_approval')
        self.assertEqual(e.changes[('b', 'one.py')]['state'], 'pending')
        self.assertFalse(e.changed)

    def test_event_dedup_and_task_isolation(self):
        e = TaskEvidence()
        event = {'type': 'git_diff', 'task_id': 'one', 'event_id': 'id', 'diff': 'actual'}
        e.apply(event); e.apply(event)
        e.apply({**event, 'event_id': 'other', 'task_id': 'two'})
        self.assertEqual(len(e.diffs), 1)

    def test_recovery_retains_configuration_and_unknown_outcome(self):
        events = [{'type': 'development_task_started', 'task_id': 't', 'phase': 'executing'},
            {'type': 'tool_started', 'task_id': 't', 'action_id': 'a', 'tool': 'write_file'}]
        e = TaskEvidence.from_events(events, recovered=True)
        self.assertEqual(e.phase, 'interrupted')
        self.assertEqual(e.actions['a']['status'], 'unknown')

    def test_review_has_real_identity_and_output(self):
        e = TaskEvidence()
        e.apply({'type': 'development_agent_config', 'agent': 'Review Agent', 'task_id': 't',
                 'selected_model': ['reviewer', 'review'], 'model_source': 'independent role'})
        for kind in ('review_started', 'review_result'):
            e.apply({'type': kind, 'task_id': 't', 'run_id': 'parent', 'message_id': 'parent',
                     'review_id': 'review-run', 'review': {'model': 'actual-review', 'text': 'findings'}})
        row = e.runs[('Review Agent', 'review-run')]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['parent_run_id'], 'parent')
        self.assertEqual(row['model'], 'actual-review')


class RuntimeUXTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.path = self.project / 'value.py'
        self.path.write_bytes(b'VALUE = 1\n')
        self.rt = DevelopmentRuntime(ModeState(self.root / 'home'))
        self.rt.initialize(self.project)
        self.rt.state.switch('development')
        self.rt.bridge.agent.thinking_mode = 'off'
        self.rt.bridge.agent.thinking = False
        self.addCleanup(self.rt.close)

    def edit(self, content='VALUE = 2\n'):
        self.rt.bridge.router = Router([call('read_file', path='value.py'),
            call('write_file', path='value.py', content=content), 'actual completion'])
        result = self.rt.run('edit value', permission='workspace-write', planning='none')
        self.assertTrue(result['ok'], result)
        return next(iter(self.rt.evidence.changes.values()))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.project, check=True, capture_output=True)

    def test_written_diff_counts_checkpoint_and_restore_are_real(self):
        change = self.edit()
        self.assertEqual(change['state'], 'written')
        self.assertEqual((change['added'], change['removed']), (1, 1))
        self.assertIn('+VALUE = 2', change['diff'])
        self.assertTrue(change['checkpoint'])
        self.assertEqual(change['task_id'], self.rt.last_task_id)
        preview = self.rt.checkpoint_preview(change['checkpoint'], change['path'])
        self.assertEqual(preview['expected'], change['revision'])
        result = self.rt.restore_checkpoint(change['checkpoint'], change['path'], preview['expected'], permission='workspace-write')
        self.assertTrue(result['ok'], result)
        self.assertTrue(change['revision_changed'])
        self.assertEqual(self.path.read_text(), 'VALUE = 1\n')

    def test_committed_only_when_head_has_the_recorded_version(self):
        self.git('init', '-q'); self.git('config', 'core.autocrlf', 'false')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', '.'); self.git('commit', '-qm', 'before')
        change = self.edit()
        self.rt.refresh_change_states()
        self.assertEqual(change['state'], 'written')
        self.git('add', '.'); self.git('commit', '-qm', 'actual Agent edit')
        self.rt.refresh_change_states()
        self.assertEqual(change['state'], 'committed')
        self.assertEqual(change['commit'], self.git('rev-parse', 'HEAD').stdout.decode().strip())
        self.path.write_bytes(b'VALUE = 3\n')
        self.rt.refresh_change_states()
        self.assertEqual(change['state'], 'committed')
        self.assertTrue(change['revision_changed'])

    def test_clean_repository_does_not_mean_agent_change_was_committed(self):
        self.git('init', '-q'); self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', '.'); self.git('commit', '-qm', 'before')
        change = self.edit()
        self.git('checkout', '--', 'value.py')
        self.rt.refresh_change_states()
        self.assertEqual(change['state'], 'written')
        self.assertTrue(change['revision_changed'])

    def test_secret_diff_is_opaque_and_invalid_text_is_unavailable(self):
        guard = FileRevisionGuard(self.project)
        secret = self.project / 'config.json'
        token = 'test-credential-unique-abcdef1234567890'
        secret.write_text(json.dumps({'api_key': token, 'enabled': True}))
        value = guard._public_text(str(secret))
        self.assertNotIn(token, value)
        self.assertIn('REDACTED_SECRET', value)
        self.path.write_bytes(b'\0binary')
        self.assertIsNone(guard._public_text(str(self.path)))

    def test_future_model_settings_leave_frozen_task_unchanged(self):
        self.rt.state.configure_agent_model('forge/sub1', ('p', 'one'))
        snap = self.rt.state.task_snapshot()
        self.rt.state.configure_agent_model('forge/sub1', ('q', 'two'))
        self.rt.state.configure_agent_model('forge/sub2', ('p', 'three'))
        self.assertEqual(dict(snap.agent_models), {'forge/sub1': ('p', 'one')})
        current = dict(self.rt.state.task_snapshot().agent_models)
        self.assertEqual(current, {'forge/sub1': ('q', 'two'), 'forge/sub2': ('p', 'three')})

    def test_actual_main_child_review_routes_match_configuration_evidence(self):
        from forge.model import ModelRouter, Provider
        from forge.test_phase_models import Transport
        transport = Transport([call('spawn_subagent', task='inspect source', mode='plan'),
            'actual child output', 'actual main output', 'actual review output'])
        providers = [Provider(p, 'https://' + p + '.invalid', default_model=m)
            for p, m in [('writer', 'code'), ('child', 'small'), ('reviewer', 'review')]]
        router = ModelRouter(providers, transport=transport, primary=('writer', 'code'), retries_per_provider=0)
        self.rt.bridge.router = self.rt.bridge.agent.phase_catalog = router
        self.rt.catalog = self.rt.bridge.models = {p.name: (p.name, p.default_model) for p in providers}
        self.rt.state.configure_role('default', ('writer', 'code'))
        self.rt.state.configure_agent_model('forge/sub1', ('child', 'small'))
        self.rt.state.configure_role('review', ('reviewer', 'review'))
        result = self.rt.run('delegate through existing runtime', planning='none')
        self.assertTrue(result['ok'], result)
        self.rt.review()
        self.assertEqual([request[0] for request in transport.calls], ['writer', 'child', 'writer', 'reviewer'])
        config = self.rt.evidence.configurations
        self.assertEqual(config['forge/sub1']['model_source'], 'independent')
        self.assertEqual(config['Review Agent']['selected_model'], ['reviewer', 'review'])
        child = next(row for row in self.rt.evidence.runs.values() if row['agent'] == 'forge/sub1')
        self.assertEqual(child['model'], 'small')
        self.assertTrue(child['parent_run_id'])
        events = self.rt.recovery(self.rt.bridge.agent.session.path)['events']
        self.assertTrue(any(e['type'] == 'development_agent_config' and e['agent'] == 'forge/sub1' for e in events))


if __name__ == '__main__':
    unittest.main()
