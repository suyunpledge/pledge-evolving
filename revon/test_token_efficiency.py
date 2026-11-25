"""Append-only scoped guidance and nonduplicated task boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from .compaction import ContextBudget
from .context_transaction import valid_tool_pairs
from .loop import Agent, LoopLimits
from .model import Completion, Usage
from .policy import Mode, Policy
from .task_contract import extract_task_contract
from .test_project_rules import Router
from .tools import build_builtin_registry, ToolSpec, ToolResult
from .policy import ToolEffect


def read(path, wire='openai'):
    return Completion('', Usage(), wire=wire, tool_calls=[{
        'id': 'read-' + path, 'name': 'read_file', 'args': {'path': path}, 'wire': wire}])


class TokenEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'project'
        self.ws.mkdir()
        for name, text in [('AGENTS.md', 'ROOT unique review guidance'),
                           ('src/AGENTS.md', 'SOURCE unique scope guidance'),
                           ('lib/AGENTS.md', 'LIBRARY unique scope guidance'),
                           ('src/a.py', 'a = 1'), ('lib/b.py', 'b = 2')]:
            path = self.ws / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def agent(self, router, **kwargs):
        agent = Agent(home=self.root / 'home', workspace=self.ws, router=router,
            registry=build_builtin_registry(), policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY),
            limits=LoopLimits(context_chars=100000, max_steps=8), **kwargs)
        self.addCleanup(agent.secret_scope.close)
        return agent

    def test_discovery_appends_only_new_entries_and_preserves_sent_prefix(self):
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire):
                router = Router([read('src/a.py', wire), read('lib/b.py', wire), 'done'])
                agent = self.agent(router, wire_hint=wire)
                result = agent.run('Review both files.')
                self.assertEqual(result.stopped, 'final')
                for index, messages in enumerate(router.calls):
                    for marker in ['ROOT unique', 'SOURCE unique', 'LIBRARY unique'][:index+1]:
                        self.assertEqual(str(messages).count(marker), 1)
                    self.assertTrue(valid_tool_pairs(messages))
                    if index:
                        previous = router.calls[index-1]
                        self.assertEqual(messages[:len(previous)], previous)
                self.assertEqual(len(agent._rule_fragments), 3)
                self.assertIn('supplement', agent._rule_fragments[-1])

    def test_changed_or_removed_parent_clears_all_old_fragments(self):
        for change in ('change', 'remove'):
            with self.subTest(change=change):
                path = self.ws / 'AGENTS.md'
                path.write_text('OLD_ROOT review guidance', encoding='utf-8')
                agent = self.agent(Router([]))
                messages = [{'role': 'system', 'content': 'system'}]
                agent._refresh_project_rules(messages)
                agent._prefix_sent = len(messages)
                agent._rule_targets.append('src/a.py')
                agent._refresh_project_rules(messages)
                self.assertEqual(str(messages).count('OLD_ROOT'), 1)
                agent._recent_cache_rate = .95
                agent._cache_evidence = {'rate': .95}
                if change == 'change':
                    path.write_text('NEW_ROOT review guidance', encoding='utf-8')
                else:
                    path.unlink()
                agent._refresh_project_rules(messages)
                self.assertNotIn('OLD_ROOT', str(messages))
                self.assertEqual(str(messages).count('SOURCE unique'), 1)
                self.assertEqual(len(agent._rule_fragments), 1)
                self.assertIsNone(agent._recent_cache_rate)
                self.assertIsNone(agent._cache_evidence)

    def test_first_scoped_discovery_without_root_also_preserves_sent_prefix(self):
        (self.ws / 'AGENTS.md').unlink()
        for wire in ('openai', 'anthropic'):
            router = Router([read('src/a.py', wire), 'done'])
            agent = self.agent(router, wire_hint=wire)
            self.assertEqual(agent.run('Inspect the source.').stopped, 'final')
            self.assertEqual(router.calls[1][:len(router.calls[0])], router.calls[0])
            self.assertEqual(str(router.calls[1]).count('SOURCE unique'), 1)
            self.assertTrue(valid_tool_pairs(router.calls[1]))

    def test_fragments_survive_compaction_and_continuation_without_full_reinjection(self):
        router = Router([read('src/a.py'), read('lib/b.py'), 'done', 'next'])
        agent = self.agent(router)
        agent.compactor = ContextBudget(max_chars=3000, keep_tail=2)
        first = agent.run('Review both files.', history=[{'role': 'assistant', 'content': 'old ' * 2500}]*8)
        self.assertEqual(first.stopped, 'final')
        self.assertTrue(any(e.get('committed') for e in first.events if e['type'] == 'compaction'))
        sent = copy.deepcopy(router.calls[-1])
        fragments = list(agent._rule_fragments)
        agent.compactor.max_chars = 100000
        second = agent.run('Explain the review.', history=[
            *agent._chat_history, {'role': 'user', 'content': 'Review both files.'},
            {'role': 'assistant', 'content': first.text}])
        self.assertEqual(second.stopped, 'final')
        self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
        self.assertEqual(router.calls[-1][:len(sent)], sent)
        self.assertEqual(agent._rule_fragments, fragments)
        self.assertEqual(str(router.calls[-1]).count('ROOT unique'), 1)

    def test_planner_receives_one_complete_current_rules_snapshot(self):
        router = Router([read('src/a.py'), read('lib/b.py'), 'done'])
        agent = self.agent(router)
        first = agent.run('Review both files.')
        router.answers = iter([json.dumps({'tasks': ['Explain']}), 'explained'])
        agent.planning_level = 'low'
        agent.run('Explain findings.', history=[{'role': 'user', 'content': 'Review both files.'},
                                               {'role': 'assistant', 'content': first.text}])
        planner = router.calls[-2]
        for marker in ('ROOT unique', 'SOURCE unique', 'LIBRARY unique'):
            self.assertEqual(str(planner).count(marker), 1)

    def test_current_request_has_full_task_once_but_standalone_contract_keeps_objective(self):
        task = 'unique boundary objective: inspect all records and verify each persisted checkpoint.'
        router = Router(['done'])
        agent = self.agent(router)
        result = agent.run(task)
        self.assertEqual(result.stopped, 'final')
        self.assertEqual(str(router.calls[0]).count(task), 1)
        self.assertIn('<objective>', extract_task_contract(task).render_projection())
        self.assertIn('<objective>', extract_task_contract(task).render_anchor())
        self.assertNotIn('<objective>', router.calls[0][-1]['content'])

    def test_summary_keeps_result_tail_instead_of_spending_budget_on_call_ids(self):
        for wire in ('openai', 'anthropic'):
            messages = []
            for i in range(3):
                result = json.dumps({'items': 'data ' * 1000, 'next_cursor': f'actual-cursor-{i}'})
                if wire == 'openai':
                    messages += [{'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'*500,
                        'function': {'name': 'process_batch', 'arguments': '{"cursor":"previous"}'}}]},
                        {'role': 'tool', 'tool_call_id': 'x'*500, 'content': result}]
                else:
                    messages += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'x'*500,
                        'name': 'process_batch', 'input': {'cursor': 'previous'}}]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x'*500,
                                                    'content': result}]}]
            before = copy.deepcopy(messages)
            summary = ContextBudget._excerpt(messages)
            self.assertIn('actual-cursor-2', summary)
            self.assertIn('actual-cursor-1', summary)
            self.assertIn('process_batch', summary)
            self.assertIn('unverified', summary)
            self.assertNotIn('x'*100, summary)
            self.assertLess(len(summary), 900)
            self.assertEqual(messages, before)

    def test_real_agent_summary_keeps_progress_after_a_later_read(self):
        router = Router([Completion('', Usage(), tool_calls=[{
            'id': 'page', 'name': 'next_page', 'args': {}, 'wire': 'openai'}]),
            read('src/a.py'), 'done'])
        agent = self.agent(router)
        agent.registry.register(ToolSpec('next_page', 'Read a page.',
            lambda args, ctx: ToolResult(True, json.dumps({'items': 'x'*3300,
                'next_cursor': 'ACTUAL_CONTINUATION'})), read_only=True), trusted=True, effect=ToolEffect.READ)
        agent.limits.max_tool_result = 8000
        agent.compactor = ContextBudget(max_chars=3000, keep_tail=2)
        report = agent.run('Review the pages and code.')
        self.assertEqual(report.stopped, 'final')
        summaries = [m['content'] for m in router.calls[-1] if isinstance(m.get('content'), str)
                     and m['content'].startswith('[compacted')]
        self.assertTrue(any('ACTUAL_CONTINUATION' in s for s in summaries))
        self.assertTrue(valid_tool_pairs(router.calls[-1]))

    def test_premium_finisher_receives_actual_host_task_after_scoped_discovery(self):
        from .model import Provider
        from .routing import SmartRouter, RoutingConfig
        calls = []
        class Transport:
            def complete(inner, provider, model, messages, **options):
                calls.append((model, copy.deepcopy(messages), copy.deepcopy(options)))
                if len(calls) == 1:
                    return '', Usage(1, 1, model, provider.name), {
                        'wire': 'openai', 'tool_calls': read('src/a.py').tool_calls}
                return ('draft' if model == 'draft' else 'finished'), Usage(1, 1, model, provider.name)
        router = SmartRouter([Provider('p', 'https://fixture.invalid', default_model='draft')],
            transport=Transport(), retries_per_provider=0, routing=RoutingConfig(
                strategy='premium', tiers=[('p', 'draft')], premium=[('p', 'final')]))
        agent = self.agent(router)
        result = agent.run('ACTUAL USER OBJECTIVE: review src/a.py.')
        self.assertEqual(result.stopped, 'final')
        self.assertEqual([c[0] for c in calls], ['draft', 'draft', 'final'])
        self.assertIn('SOURCE unique', str(calls[1][1]))
        self.assertIn('ACTUAL USER OBJECTIVE', calls[-1][1][1]['content'])
        self.assertNotIn('SOURCE unique', str(calls[-1][1]))
        self.assertNotIn('ROOT unique', str(calls[-1][1]))
        self.assertTrue(all('task_context' not in c[2] for c in calls))

    def test_budget_pause_keeps_complete_native_history_without_claiming_completion(self):
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire):
                router = Router([read('src/a.py', wire), 'done'])
                agent = self.agent(router, wire_hint=wire)
                agent.limits.max_steps = 1
                first = agent.run('Review the source.')
                self.assertEqual(first.stopped, 'max_steps')
                self.assertNotEqual(first.as_result().outcome, 'succeeded')
                self.assertTrue(any(e['type'] == 'cache_prefix_checkpointed' for e in first.events))
                saved = copy.deepcopy(agent._conversation_prefix._saved['messages'])
                self.assertTrue(valid_tool_pairs(saved))
                self.assertNotIn('[stopped:', str(saved))
                agent.limits.max_steps = 5
                second = agent.run('Continue reviewing.', history=[
                    {'role': 'user', 'content': 'Review the source.'},
                    {'role': 'assistant', 'content': first.text}])
                self.assertEqual(second.stopped, 'final')
                self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
                self.assertEqual(router.calls[-1][:len(saved)], saved)
                self.assertIn('a = 1', str(router.calls[-1]))


if __name__ == '__main__':
    unittest.main()
