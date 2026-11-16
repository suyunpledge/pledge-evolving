"""Recorded actions keep full output while explicit model views avoid repeated bulk data."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .artifacts import ArtifactStore
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .loop import Agent, LoopLimits
from .model import Completion, Usage
from .policy import Policy, Mode, ToolEffect
from .test_project_rules import Router
from .tools import ToolSpec, ToolResult, build_builtin_registry


class ResultViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.home = self.root / 'home'
        self.body = json.dumps({'items': 'data '*700, 'units': 42, 'next_cursor': 'next-page'})
        self.calls = 0

    def registry(self, *, expose=None, fields=('units', 'next_cursor'), ok=True, state='completed'):
        registry = build_builtin_registry(expose=expose)
        def handler(args, ctx):
            self.calls += 1
            return ToolResult(ok, self.body if ok else '', '' if ok else self.body,
                              {'execution_state': state})
        registry.register(ToolSpec('audit', 'Read audit data.', handler,
            result_fields=fields), trusted=True, effect=ToolEffect.READ)
        return registry

    def context(self, registry, *, inline_limit=8000, **policy):
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=registry,
            policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY, non_interactive=True, **policy),
            capabilities={'artifact_inline_limit': inline_limit})
        self.addCleanup(ctx.secret_scope.close)
        self.addCleanup(ctx.session.close)
        return ctx

    def test_view_retains_exact_full_source_and_action_is_not_reexecuted(self):
        ctx = self.context(self.registry())
        executor, request = ActionExecutor(), ActionRequest('audit', {})
        first = executor.execute(ctx, request).as_tool_result()
        second = executor.execute(ctx, request).as_tool_result()
        view = json.loads(first.content)
        self.assertTrue(first.ok)
        self.assertEqual(view['units'], 42)
        self.assertEqual(view['next_cursor'], 'next-page')
        self.assertNotIn('items', view)
        self.assertEqual(view['_retained']['trust'], 'untrusted')
        self.assertEqual(self.calls, 1)
        self.assertTrue(second.meta['deduplicated'])
        raw = ctx.capabilities['artifact_store']._load(view['_retained']['id'])[0]
        self.assertEqual(raw, self.body)
        self.assertEqual(first.meta['artifact']['id'], view['_retained']['id'])

    def test_selected_numbers_keep_original_precision_and_lexemes(self):
        self.body = ('{"units":123456789012345678901234567890,"ratio":1.2345678901234567890123456789,'
                     '"items":"' + 'x'*2000 + '"}')
        ctx = self.context(self.registry(fields=('units', 'ratio')))
        value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).content
        self.assertIn('123456789012345678901234567890', value)
        self.assertIn('1.2345678901234567890123456789', value)

    def test_default_inline_limit_preserves_unicode_full_output_through_real_paging(self):
        self.body = json.dumps({'items': '中文🙂'*2500, 'units': 42,
                                'next_cursor': 'next-page'}, ensure_ascii=False)
        ctx = self.context(self.registry(), inline_limit=4000)
        executor = ActionExecutor()
        result = executor.execute(ctx, ActionRequest('audit', {})).as_tool_result()
        view = json.loads(result.content)
        self.assertEqual(view['next_cursor'], 'next-page')
        parts, offset = [], 0
        while offset is not None:
            page = executor.execute(ctx, ActionRequest('artifact_read', {
                'id': view['_retained']['id'], 'offset': offset, 'limit': 3000})).as_tool_result()
            self.assertTrue(page.ok)
            self.assertNotIn('artifact', page.meta)
            parts.append(page.content.split('\n', 1)[1])
            offset = page.meta['next_offset']
        self.assertEqual(''.join(parts), self.body)

    def test_small_or_unencodable_view_falls_back_without_adding_overhead(self):
        for body in ('{"units":42,"next_cursor":"next"}',
                     '{"units":42,"next_cursor":"\\ud800","items":"' + 'x'*500 + '"}'):
            self.body = body
            ctx = self.context(self.registry())
            value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
            self.assertTrue(value.ok)
            self.assertEqual(value.content, body)
            self.assertNotIn('artifact', value.meta)

    def test_protected_source_and_view_do_not_export_raw_secrets(self):
        secret = 'fixture-view-sensitive-572938'
        self.body = json.dumps({'items': 'x'*2000, 'units': 42, 'next_cursor': secret})
        ctx = self.context(self.registry())
        ctx.secret_scope.reference(secret)
        value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
        self.assertNotIn(secret, value.content)
        self.assertIn('SECRET_REF:', value.content)
        source = ctx.capabilities['artifact_store']._load(value.meta['artifact']['id'])[0]
        self.assertNotIn(secret, source)

    def test_optional_retrieval_policy_error_does_not_change_completed_action(self):
        ctx = self.context(self.registry())
        evaluate = Policy.evaluate
        def gated(policy, name, **options):
            if name == 'artifact_read':
                raise RuntimeError('fixture optional presentation policy')
            return evaluate(policy, name, **options)
        with patch.object(Policy, 'evaluate', gated):
            value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
        self.assertTrue(value.ok)
        self.assertEqual(value.content, self.body)

    def test_ambiguous_invalid_or_missing_fields_leave_output_unchanged(self):
        for body in ['{"units":1,"units":2}', '{"units":NaN}', 'not JSON', '[]', '{"other":3}']:
            with self.subTest(body=body):
                self.body = body
                ctx = self.context(self.registry())
                value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
                self.assertTrue(value.ok)
                self.assertEqual(value.content, body)
                self.assertNotIn('model_view', value.meta)

    def test_hidden_denied_or_ask_reader_does_not_activate_projection(self):
        for kind in ('hidden', 'denied', 'ask'):
            with self.subTest(kind=kind):
                registry = self.registry(expose=['audit'] if kind == 'hidden' else None)
                ctx = self.context(registry, **({'deny': ('artifact_read',)} if kind == 'denied' else
                                              {'ask': ('artifact_read',)} if kind == 'ask' else {}))
                value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
                self.assertEqual(value.content, self.body)
                self.assertNotIn('model_view', value.meta)

    def test_reader_name_alone_does_not_enable_output_projection(self):
        registry = self.registry()
        registry.register(ToolSpec('artifact_read', 'Unverified replacement',
            lambda a,c: ToolResult(True, 'not an artifact reader')), force=True)
        ctx = self.context(registry, allow=('artifact_read',))
        value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
        self.assertEqual(value.content, self.body)
        self.assertNotIn('model_view', value.meta)

    def test_failed_or_unknown_action_is_never_represented_as_a_successful_view(self):
        for ok, state in ((False, 'completed'), (True, 'unknown')):
            ctx = self.context(self.registry(ok=ok, state=state))
            value = ActionExecutor().execute(ctx, ActionRequest('audit', {}))
            self.assertEqual(value.content or value.error, self.body)
            self.assertNotIn('model_view', value.meta)
        registry = self.registry()
        registry.register(ToolSpec('diagnostic', '', lambda a,c: ToolResult(True, self.body,
            error='Host diagnostic must remain available'), result_fields=('units', 'next_cursor')),
            trusted=True, effect=ToolEffect.READ)
        value = ActionExecutor().execute(self.context(registry), ActionRequest('diagnostic', {}))
        self.assertEqual(value.content, self.body)
        self.assertEqual(value.error, 'Host diagnostic must remain available')
        self.assertNotIn('model_view', value.meta)

    def test_retention_failure_keeps_action_outcome_and_full_available_content(self):
        ctx = self.context(self.registry())
        with patch.object(ArtifactStore, 'put', side_effect=OSError('fixture disk unavailable')):
            value = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
        self.assertTrue(value.ok)
        self.assertEqual(value.content, self.body)
        self.assertEqual(value.meta['artifact_retention']['status'], 'failed')
        self.assertNotIn('model_view', value.meta)
        self.assertEqual(self.calls, 1)
        with patch('forge.result_view.select_fields', side_effect=RuntimeError('fixture presentation failure')):
            fallback = ActionExecutor().execute(ctx, ActionRequest('audit', {})).as_tool_result()
        self.assertTrue(fallback.ok)
        self.assertEqual(fallback.content, self.body)
        self.assertNotIn('model_view', fallback.meta)

    def test_fields_are_host_owned_cloned_and_revoked(self):
        registry = self.registry()
        child = registry.clone()
        registry.unregister('audit')
        self.assertEqual(registry.result_fields('audit'), ())
        self.assertEqual(child.result_fields('audit'), ('units', 'next_cursor'))
        with self.assertRaises(ValueError):
            registry.register(ToolSpec('unsafe', '', lambda a,c: ToolResult(True), result_fields=('units',)))
        with self.assertRaises(ValueError):
            registry.register(ToolSpec('unsafe', '', lambda a,c: ToolResult(True), result_fields=('units',)), trusted='yes')
        for fields in ('units', ('units', 'units'), ('_retained',), (None,)):
            with self.assertRaises(ValueError):
                registry.register(ToolSpec('bad', '', lambda a,c: ToolResult(True), result_fields=fields), trusted=True)

    def test_native_model_can_read_full_source_after_receiving_a_view(self):
        for wire in ('openai', 'anthropic'):
            registry = self.registry()
            owner = self
            class ReadRouter(Router):
                def complete(inner, messages, **options):
                    if len(inner.calls) == 0:
                        call = {'id': 'audit', 'name': 'audit', 'args': {}, 'wire': wire}
                    elif len(inner.calls) == 1:
                        content = messages[-1]['content']
                        text = content[0]['content'] if wire == 'anthropic' else content
                        view = json.loads(text)
                        owner.assertNotIn('items', view)
                        call = {'id': 'raw', 'name': 'artifact_read', 'args': {
                            'id': view['_retained']['id'], 'limit': 3000}, 'wire': wire}
                    else:
                        owner.assertIn('data data', str(messages[-1]))
                        return super().complete(messages, **options)
                    inner.calls.append(json.loads(json.dumps(messages)))
                    return Completion('', Usage(), wire=wire, tool_calls=[call])
            router = ReadRouter(['verified'])
            agent = Agent(home=self.home, workspace=self.ws, router=router, registry=registry,
                policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY), project_rules=False,
                wire_hint=wire, limits=LoopLimits(max_steps=4, max_tool_result=8000))
            self.addCleanup(agent.secret_scope.close)
            result = agent.run('Inspect the full audit data.')
            self.assertEqual(result.stopped, 'final')
            self.assertEqual([s.tool for s in result.steps], ['audit', 'artifact_read'])


if __name__ == '__main__':
    unittest.main()
