"""Real retained-output lifecycle; fixtures are local and require no model API."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from .artifacts import ArtifactStore, MAX_BYTES
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .loop import Agent, LoopLimits
from .model import Completion, Usage
from .policy import Policy, Mode, ToolEffect
from .tools import ToolSpec, ToolResult, build_builtin_registry


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / 'project'
        self.ws.mkdir()
        self.home = self.root / 'home'
        self.store = ArtifactStore(self.home, self.ws)

    def context(self, text='x'*6000, *, ok=True, deny=()):
        reg = build_builtin_registry()
        reg.register(ToolSpec('large_observation', '', lambda a, c: ToolResult(ok, text,
            meta={'execution_state': 'completed'})), trusted=True, effect=ToolEffect.READ)
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=reg,
            policy=Policy(mode=Mode.READ_ONLY, workspace=self.ws, deny=deny, non_interactive=True))
        self.addCleanup(ctx.secret_scope.close)
        self.addCleanup(ctx.session.close)
        return ctx

    def test_content_identity_deduplicates_and_merges_provenance(self):
        a = self.store.put('same', source='one', action_id='a')
        b = self.store.put('same', source='two', action_id='b')
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(len(list(self.store.root.glob('a_*'))), 2)
        self.assertEqual(len(b['sources']), 2)

    def test_unicode_paging_roundtrips_a_single_long_line(self):
        text = '中文🙂x'*2300
        meta = self.store.put(text)
        parts, offset = [], 0
        while offset is not None:
            page = self.store.read(meta['id'], offset=offset, limit=777)
            parts.append(page['text'])
            offset = page['next_offset']
        self.assertEqual(''.join(parts), text)

    def test_grep_paginates_literal_matches_without_regex_execution(self):
        meta = self.store.put('a.*b|a.*b|a.*b')
        first = self.store.grep(meta['id'], '.*', limit=2)
        last = self.store.grep(meta['id'], '.*', offset=first['next_offset'])
        self.assertEqual([r['offset'] for r in first['matches']+last['matches']], [1, 6, 11])
        self.assertIsNone(last['next_offset'])

    def test_invalid_handles_and_ranges_fail_closed(self):
        identifier = self.store.put('abc')['id']
        for bad in ['../outside', 'a_'+64*'g', '', None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.read(bad)
        for args in [{'offset': -1}, {'offset': True}, {'offset': 4}, {'limit': 0}, {'limit': 4001}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.read(identifier, **args)

    def test_workspace_scope_and_digest_are_verified(self):
        meta = self.store.put('private output')
        other = ArtifactStore(self.home, self.root / 'another-project')
        with self.assertRaises(FileNotFoundError):
            other.read(meta['id'])
        (self.store.root / meta['id']).write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.store.read(meta['id'])

    def test_partial_commit_and_oversize_never_claim_retention(self):
        with self.assertRaises(ValueError):
            self.store.put('x'*(MAX_BYTES+1))
        meta = self.store.put('complete')
        (self.store.root / (meta['id']+'.json')).unlink()
        with self.assertRaises(FileNotFoundError):
            self.store.read(meta['id'])

    def test_action_retains_protected_output_without_writing_workspace(self):
        secret = 'fixture-secret-98483'
        ctx = self.context('head ' + secret + 'x'*6000 + ' needle')
        ctx.secret_scope.reference(secret)
        result = ActionExecutor().execute(ctx, ActionRequest('large_observation', {})).as_tool_result()
        self.assertTrue(result.ok)
        meta = result.meta['artifact']
        self.assertLess(len(result.content), 4000)
        text = self.store._load(meta['id'])[0]
        self.assertNotIn(secret, text)
        self.assertIn('needle', text)
        self.assertFalse(list(self.ws.iterdir()))

    def test_retention_failure_preserves_action_and_deduplication(self):
        ctx, executor = self.context(), ActionExecutor()
        request = ActionRequest('large_observation', {})
        with patch.object(ctx.capabilities['artifact_store'], 'put', side_effect=RuntimeError('disk fixture')):
            first = executor.execute(ctx, request).as_tool_result()
        second = executor.execute(ctx, request).as_tool_result()
        self.assertTrue(first.ok)
        self.assertTrue(second.meta['deduplicated'])
        self.assertEqual(first.meta['artifact_retention']['status'], 'failed')

    def test_large_known_failure_keeps_completed_failure_state(self):
        ctx = self.context(ok=False)
        result = ActionExecutor().execute(ctx, ActionRequest('large_observation', {}))
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.execution_state, 'completed')
        self.assertIn('artifact', result.meta)

    def test_artifact_tools_obey_deny_policy(self):
        ctx = self.context(deny=('artifact_read',))
        identifier = self.store.put('text')['id']
        result = ActionExecutor().execute(ctx, ActionRequest('artifact_read', {'id': identifier}))
        self.assertEqual(result.authorization, 'deny')

    def test_artifact_tools_are_bounded_and_never_recursively_retained(self):
        ctx = self.context()
        identifier = self.store.put('prefix\n' + 'needle'*2000)['id']
        executor = ActionExecutor()
        read = executor.execute(ctx, ActionRequest('artifact_read', {'id': identifier, 'limit': 3000})).as_tool_result()
        found = executor.execute(ctx, ActionRequest('artifact_grep', {'id': identifier, 'pattern': 'needle'})).as_tool_result()
        self.assertTrue(read.ok)
        self.assertTrue(found.ok)
        self.assertLess(len(read.content), 4000)
        self.assertLess(len(found.content), 4000)
        self.assertNotIn('artifact', read.meta)
        self.assertIsNotNone(found.meta['next_offset'])

    def test_real_agent_can_find_and_read_beyond_initial_preview(self):
        ctx = self.context('start ' + 'x'*7000 + 'needle AT THE END')
        owner = self
        class Router:
            def __init__(self): self.count = 0
            def complete(self, messages, **kwargs):
                self.count += 1
                if self.count == 1:
                    call = {'tool': 'large_observation', 'args': {}}
                elif self.count == 2:
                    identifier = re.search(r'a_[0-9a-f]{64}', messages[-1]['content']).group()
                    call = {'tool': 'artifact_grep', 'args': {'id': identifier, 'pattern': 'needle'}}
                    self.identifier = identifier
                elif self.count == 3:
                    call = {'tool': 'artifact_read', 'args': {'id': self.identifier, 'offset': 7006}}
                else:
                    owner.assertIn('needle AT THE END', messages[-1]['content'])
                    return Completion('verified', Usage(10, 2, 'fake', 'fixture'))
                return Completion(json.dumps(call), Usage(10, 2, 'fake', 'fixture'))
        agent = Agent(home=self.home, workspace=self.ws, registry=ctx.registry,
                      policy=ctx.policy, router=Router(), limits=LoopLimits(max_steps=5), project_rules=False)
        result = agent.run('inspect the end of the observation')
        self.assertEqual(result.stopped, 'final')
        self.assertEqual([s.tool for s in result.steps], ['large_observation', 'artifact_grep', 'artifact_read'])
        self.assertFalse(list(self.ws.iterdir()))

    def test_coding_profile_exposes_retrieval_in_both_bundle_locations(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ['bundles/modes/coding.json', 'forge/bundles/modes/coding.json']:
            rows = json.loads((root / relative).read_text(encoding='utf-8'))
            config = next(row['config'] for row in rows if row['id'] == 'tools')
            registry = build_builtin_registry(expose=config['expose'])
            self.assertIn('artifact_read', registry.names())
            self.assertIn('artifact_grep', registry.names())

    def test_agent_reports_failed_retention_without_retry_or_workspace_fallback(self):
        from .test_task_planning import Router
        ctx = self.context()
        agent = Agent(home=self.home, workspace=self.ws, registry=ctx.registry,
            policy=ctx.policy, router=Router([
                json.dumps({'tool': 'large_observation', 'args': {}}), 'done']), project_rules=False)
        with patch.object(ArtifactStore, 'put', side_effect=OSError('disk fixture')) as put:
            report = agent.run('inspect output')
        self.assertEqual(put.call_count, 1)
        self.assertEqual(report.stopped, 'final')
        self.assertIn('Output retention failed', report.steps[0].result)
        self.assertNotIn('artifact', report.steps[0].meta)
        self.assertFalse(list(self.ws.iterdir()))


if __name__ == '__main__':
    unittest.main()
