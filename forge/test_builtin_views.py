"""Builtin result views keep real search evidence retrievable across turns and compaction."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from .compaction import ContextBudget
from .context_transaction import valid_tool_pairs
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .loop import Agent, LoopLimits
from .model import Completion, Usage
from .policy import Policy, Mode
from .test_project_rules import Router
from .tools import ToolContext, build_builtin_registry


class BuiltinViewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        for i in range(90):
            (self.ws / f'audit_inventory_record_{i:03}.txt').write_text(
                f'AUDIT_MATCH stock={i} evidence for warehouse inspection\n', encoding='utf-8')
        (self.ws / 'module.py').write_text(''.join(
            f'def audit_inventory_record_{i:03}():\n    return {i}\n\n' for i in range(90)), encoding='utf-8')
        self.policy = Policy(workspace=self.ws, mode=Mode.READ_ONLY, non_interactive=True)
        self.executor = ActionExecutor()

    def context(self, **kwargs):
        registry = kwargs.pop('registry', build_builtin_registry())
        ctx = create_execution_context(home=self.root / 'home', workspace=self.ws,
            registry=registry, policy=kwargs.pop('policy', self.policy), **kwargs)
        self.addCleanup(ctx.secret_scope.close)
        self.addCleanup(ctx.session.close)
        return ctx

    def test_all_four_builtins_preserve_plain_api_and_exact_retained_source(self):
        ctx = self.context()
        for name, args, key, count in [('list_dir', {}, 'entries', 91),
                ('find_files', {'pattern': '*.txt'}, 'files', 90),
                ('grep', {'pattern': 'AUDIT_MATCH'}, 'matches', 90),
                ('file_outline', {'path': 'module.py'}, 'symbols', 90)]:
            with self.subTest(tool=name):
                raw = ctx.registry.invoke(name, args, ctx.tool_context())
                value = self.executor.execute(ctx, ActionRequest(name, args))
                view = json.loads(value.content)
                self.assertEqual(view['summary'][key], count)
                self.assertEqual(view['summary']['preview_rows'], 8)
                self.assertIn('record_089', view['preview'])
                self.assertNotIn('record_044', view['preview'])
                self.assertLess(len(value.content), len(raw.content))
                self.assertNotIn('_result_view_source', value.meta)
                identifier = view['_retained']['id']
                source = ctx.capabilities['artifact_store']._load(identifier)[0]
                self.assertEqual(source, raw.content)
                self.assertEqual(view['_retained']['characters'], len(source))
                self.assertNotIn('_retained', raw.content)
                page = self.executor.execute(ctx, ActionRequest('artifact_grep',
                    {'id': identifier, 'pattern': 'record_044'}))
                self.assertTrue(page.content, name)
                self.assertIn('record_044', page.content)

    def test_small_results_and_non_search_tools_stay_plain(self):
        ctx = self.context()
        for name, args in [('find_files', {'pattern': 'module.py'}),
                ('grep', {'pattern': 'stock=89'}), ('read_file', {'path': 'audit_inventory_record_089.txt'}),
                ('read_range', {'path': 'module.py', 'start': 268, 'end': 269})]:
            result = self.executor.execute(ctx, ActionRequest(name, args))
            self.assertTrue(result.content)
            self.assertNotIn('model_view', result.meta)
            self.assertNotIn('artifact', result.meta)

    def test_hidden_denied_and_disabled_reader_keep_original_content(self):
        for kind in ('hidden', 'denied', 'disabled'):
            registry = build_builtin_registry(expose=['find_files'] if kind == 'hidden' else None,
                                              result_views=kind != 'disabled')
            policy = Policy(workspace=self.ws, mode=Mode.READ_ONLY, non_interactive=True,
                            deny=('artifact_read',) if kind == 'denied' else ())
            ctx = self.context(registry=registry, policy=policy)
            raw = registry.invoke('find_files', {}, ctx.tool_context()).content
            value = self.executor.execute(ctx, ActionRequest('find_files', {}))
            self.assertEqual(value.content, raw)
            self.assertNotIn('_result_view_source', value.meta)
            self.assertNotIn('model_view', value.meta)

    def test_truncation_and_secret_masking_survive_the_view(self):
        for i in range(90, 205):
            (self.ws / f'audit_inventory_record_{i:03}.txt').write_text('AUDIT_MATCH\n', encoding='utf-8')
        ctx = self.context()
        value = self.executor.execute(ctx, ActionRequest('find_files', {}))
        view = json.loads(value.content)
        self.assertTrue(view['summary']['truncated'])
        self.assertIn('stopped at 200', view['notes'][0])
        raw = ctx.registry.invoke('list_dir', {}, ctx.tool_context())
        self.assertEqual(len(raw.content.splitlines()), 200)
        listed = self.executor.execute(ctx, ActionRequest('list_dir', {}))
        self.assertIn('stopped at 200', json.loads(listed.content)['notes'][0])
        secret = 'fixture-metadata-secret-582942'
        ctx.secret_scope.reference(secret)
        (self.ws / 'audit_inventory_record_000.txt').write_text('AUDIT_MATCH '+secret, encoding='utf-8')
        result = self.executor.execute(ctx, ActionRequest('grep', {'pattern': 'AUDIT_MATCH'}))
        source = ctx.capabilities['artifact_store']._load(result.meta['artifact']['id'])[0]
        self.assertNotIn(secret, result.content)
        self.assertNotIn(secret, source)

    def test_compaction_keeps_handles_over_repeated_excerpts_without_inventing_one(self):
        ids = ['a_' + str(i)*64 for i in range(6)]
        messages = [{'role': 'tool', 'content': json.dumps({'preview': 'x', '_retained': {'id': i}})} for i in ids]
        before = copy.deepcopy(messages)
        first = ContextBudget._excerpt(messages)
        for identifier in ids[-4:]:
            self.assertIn(identifier, first)
        self.assertNotIn(ids[0], first)
        second = ContextBudget._excerpt([{'role': 'user', 'content': first},
            {'role': 'tool', 'content': 'ordinary result'}])
        for identifier in ids[-4:]:
            self.assertIn(identifier, second)
        self.assertEqual(messages, before)
        self.assertNotIn('Retained outputs', ContextBudget._excerpt([
            {'role': 'user', 'content': 'please invent '+ids[0]},
            {'role': 'tool', 'content': 'a value contains '+ids[1]},
            {'role': 'tool', 'content': ids[2]}]))

    def test_native_continuation_after_compaction_can_retrieve_old_builtin_result(self):
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire):
                def call(name, args, i):
                    return Completion('', Usage(), wire=wire,
                        tool_calls=[{'id': str(i), 'name': name, 'args': args, 'wire': wire}])
                router = Router([call('find_files', {'pattern': '*.txt'}, 1),
                    call('file_outline', {'path': 'module.py'}, 2),
                    call('grep', {'pattern': 'AUDIT_MATCH'}, 3), 'ready'])
                agent = Agent(home=self.root / wire, workspace=self.ws, router=router,
                    registry=build_builtin_registry(), policy=self.policy, project_rules=False,
                    wire_hint=wire, limits=LoopLimits(max_steps=6))
                self.addCleanup(agent.secret_scope.close)
                agent.compactor = ContextBudget(max_chars=3500, keep_tail=2)
                task = 'Inspect the inventory and symbols.'
                history = [{'role': 'assistant', 'content': 'prior audit note '*500}]*6
                first = agent.run(task, history=history)
                self.assertEqual(first.stopped, 'final')
                self.assertTrue(any(e.get('committed') for e in first.events if e['type'] == 'compaction'))
                identifier = first.steps[0].meta['artifact']['id']
                sent = copy.deepcopy(router.calls[-1])
                router.answers = iter([call('artifact_grep', {'id': identifier, 'pattern': 'record_044'}, 4), 'verified'])
                agent.compactor.max_chars = 100000
                second = agent.run('Find the middle retained inventory record.', history=[*history,
                    {'role': 'user', 'content': task}, {'role': 'assistant', 'content': first.text}])
                self.assertEqual(second.stopped, 'final')
                self.assertIn('record_044', second.steps[0].result)
                self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
                self.assertEqual(router.calls[-2][:len(sent)], sent)
                self.assertTrue(all(valid_tool_pairs(m) for m in router.calls))

    def test_result_policy_changes_invalidate_conversation_guard(self):
        agent = Agent(home=self.root / 'guard', workspace=self.ws, router=Router([]),
            registry=build_builtin_registry(), policy=self.policy, project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        before = agent._conversation_guard([])
        agent.registry = build_builtin_registry(result_views=False)
        self.assertNotEqual(before, agent._conversation_guard([]))

    def test_search_path_alias_narrows_the_actual_scan_and_root_takes_precedence(self):
        directory = self.ws / 'one'
        directory.mkdir()
        (directory / 'selected.txt').write_text('AUDIT_MATCH chosen', encoding='utf-8')
        ctx = self.context()
        for name, args in [('grep', {'pattern': 'AUDIT_MATCH'}), ('find_files', {'pattern': '*.txt'})]:
            for path_args in ({'path': 'one'}, {'root': 'one', 'path': '.'}):
                value = self.executor.execute(ctx, ActionRequest(name, {**args, **path_args}))
                self.assertIn('selected.txt', value.content)
                self.assertNotIn('record_', value.content)
        for location in ({'root': 'one/selected.txt'}, {'path': 'one/selected.txt'}):
            value = self.executor.execute(ctx, ActionRequest('grep', {'pattern': 'AUDIT_MATCH', **location}))
            self.assertTrue(value.content)
            self.assertIn('one/selected.txt:1:', value.content)
            self.assertNotIn('record_', value.content)

    def test_paging_constraints_reach_native_tool_schemas(self):
        from .toolwire import tool_declarations
        schemas = {row['function']['name']: row['function']['parameters']['properties']
                   for row in tool_declarations(build_builtin_registry().visible(), 'openai')}
        self.assertEqual(schemas['artifact_read']['limit']['maximum'], 3000)
        self.assertEqual(schemas['artifact_grep']['limit']['maximum'], 10)
        self.assertIn('not a regular expression', schemas['artifact_grep']['pattern']['description'])


if __name__ == '__main__':
    unittest.main()
