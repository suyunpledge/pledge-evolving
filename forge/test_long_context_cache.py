"""长任务压缩与跨轮缓存观测，验证宿主前缀、完整交换和真实谱系。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from .compaction import ContextBudget
from .context_transaction import compact_transaction, valid_tool_pairs
from .conversation_prefix import ConversationPrefix
from .cache_metrics import foreground_hit_rate
from .loop import Agent, LoopLimits
from .model import Completion, ModelRouter, Provider, Usage, TransportError
from .policy import Mode, Policy, ToolEffect
from .tools import build_builtin_registry, ToolSpec, ToolResult


class CompactPrefixTests(unittest.TestCase):
    def setUp(self):
        self.prefix = [{'role': 'system', 'content': 'stable host'},
                       {'role': 'user', 'content': 'frozen memory'},
                       {'role': 'user', 'content': 'project guidance'}]

    def test_size_includes_reasoning_and_opaque_replay_fields(self):
        budget = ContextBudget()
        plain = [{'role': 'assistant', 'content': 'answer'}]
        detailed = [{**plain[0], 'reasoning_content': 'r' * 5000, 'encrypted_content': 'e' * 2000}]
        self.assertGreaterEqual(budget.size(detailed) - budget.size(plain), 7000)

    def test_prefix_and_latest_task_survive_without_task_being_hoisted(self):
        task = {'role': 'user', 'content': 'actual latest task'}
        messages = [*self.prefix, *[{'role': 'assistant', 'content': 'old ' * 900} for _ in range(8)], task]
        out = ContextBudget(max_chars=2500, keep_tail=6).compact(messages, max_chars=1500,
            pinned_contents=(task['content'],), prefix_messages=self.prefix)
        self.assertEqual(out[:len(self.prefix)], self.prefix)
        self.assertEqual(out[-1], task)
        self.assertLessEqual(ContextBudget().size(out), 1500)

    def test_budget_can_reduce_fixed_tail_but_keeps_last_complete_exchange(self):
        for wire in ('openai', 'anthropic'):
            messages = [*self.prefix, {'role': 'user', 'content': 'task'}]
            groups = []
            for index in range(4):
                if wire == 'openai':
                    group = [{'role': 'assistant', 'content': '', 'reasoning_content': 'thought',
                              'tool_calls': [{'id': str(index)}]},
                             {'role': 'tool', 'tool_call_id': str(index), 'content': 'data ' * 300}]
                else:
                    group = [{'role': 'assistant', 'content': [
                        {'type': 'thinking', 'thinking': 'thought', 'signature': 'signed'},
                        {'type': 'tool_use', 'id': str(index)}]},
                        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': str(index),
                                                    'content': 'data ' * 300}]}]
                groups.append(group)
                messages += group
            before = copy.deepcopy(messages)
            budget = ContextBudget(max_chars=3000, keep_tail=6)
            out = budget.compact(messages, max_chars=3000, pinned_contents=('task',), prefix_messages=self.prefix)
            self.assertTrue(valid_tool_pairs(out))
            self.assertEqual(out[-2:], groups[-1])
            self.assertLessEqual(budget.size(out), 3000)
            self.assertEqual(messages, before)

    def test_old_final_reasoning_can_be_archived_when_latest_task_is_pinned(self):
        task = {'role': 'user', 'content': 'new task'}
        messages = [*self.prefix, {'role': 'assistant', 'content': 'done', 'reasoning_content': 'x' * 20000}, task]
        out = ContextBudget(max_chars=2000).compact(messages, max_chars=1500,
            pinned_contents=('new task',), prefix_messages=self.prefix)
        self.assertEqual(out[-1], task)
        self.assertLess(ContextBudget().size(out), 1500)

    def test_transaction_rejects_changed_ordinary_memory_prefix(self):
        class Bad(ContextBudget):
            def compact(self, *args, **kwargs):
                return [self.saved[0], {'role': 'user', 'content': 'changed memory'}]
        compactor = Bad()
        compactor.saved = self.prefix
        messages = [*self.prefix, {'role': 'assistant', 'content': 'x' * 5000}]
        with tempfile.TemporaryDirectory() as tmp:
            out, audit = compact_transaction(messages, compactor, directory=tmp, max_chars=1000,
                                             prefix_messages=self.prefix)
            self.assertIs(out, messages)
            self.assertEqual(audit['reason'], 'stable_prefix_changed')
            self.assertTrue(Path(audit['archive']['path']).is_file())


class NativeToolContentTests(unittest.TestCase):
    def test_real_agent_preserves_middle_of_bounded_tool_result_for_both_wires(self):
        from .test_project_rules import Router
        payload = 'h' * 1700 + 'MIDDLE_REQUIRED_EVIDENCE' + 't' * 1700
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = build_builtin_registry(expose=['observation'])
                registry.register(ToolSpec('observation', 'Read evidence.',
                    lambda args, ctx: ToolResult(True, payload), read_only=True), trusted=True, effect=ToolEffect.READ)
                router = Router([Completion('', Usage(), wire=wire,
                    tool_calls=[{'id': 'evidence-1', 'name': 'observation', 'args': {}, 'wire': wire}]), 'done'])
                agent = Agent(home=root, workspace=root, router=router, registry=registry,
                    policy=Policy(workspace=root, mode=Mode.READ_ONLY), project_rules=False)
                try:
                    report = agent.run('Read all evidence.')
                    self.assertEqual(report.stopped, 'final')
                    message = router.calls[-1][-1]
                    actual = (message['content'][0]['content'] if wire == 'anthropic' else message['content'])
                    self.assertEqual(actual, payload)
                    self.assertEqual(report.steps[0].result, actual)
                    self.assertTrue(valid_tool_pairs(router.calls[-1]))
                finally:
                    agent.secret_scope.close()


class ContinuationCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        class Transport:
            def complete(inner, provider, model, messages, **options):
                return 'answer', Usage(100, 10, model, provider.name, cached_tokens=95, cache_observed=True)
        self.provider = Provider('p', 'https://fixture.invalid', default_model='mimo-v2.6-pro',
                                 route_capabilities={'contextWindow': 20000, 'maxOutputTokens': 1000})
        self.agent = Agent(home=self.root, workspace=self.root, registry=build_builtin_registry(),
            router=ModelRouter([self.provider], transport=Transport(), retries_per_provider=0),
            policy=Policy(workspace=self.root, mode=Mode.READ_ONLY),
            limits=LoopLimits(context_chars=5000), project_rules=False)
        self.addCleanup(self.agent.secret_scope.close)

    def first(self, large=False):
        history = [{'role': 'assistant', 'content': 'old data ' * 600} for _ in range(10)] if large else []
        report = self.agent.run('first', history=history)
        return report, [*history, {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': report.text}]

    def test_hot_observation_and_revision_survive_exact_continuation(self):
        first, history = self.first(large=True)
        self.assertTrue(any(e.get('committed') for e in first.events if e['type'] == 'compaction'))
        revision = self.agent._context_revision
        second = self.agent.run('second', history=history)
        self.assertTrue(any(e['type'] == 'cache_observation_reused' for e in second.events))
        self.assertEqual(second.usage['requests'][0]['context_revision'], revision)
        self.assertNotIn('cache_rebuild', second.usage['requests'][0])

    def test_expired_or_different_mode_observation_is_not_reused(self):
        for change in ('age', 'mode'):
            first, history = self.first()
            if change == 'age':
                self.agent._conversation_prefix._saved['continuation']['cache']['observed_at'] -= 61
            else:
                self.provider.reasoning_mode = 'off'
            report = self.agent.run('second', history=history)
            self.assertFalse(any(e['type'] == 'cache_observation_reused' for e in report.events))

    def test_cache_deferral_has_a_bounded_soft_growth_limit(self):
        self.agent._recent_cache_rate = .99
        budget = {'input_budget': 100000, 'estimated_input_tokens': 1000}
        self.assertIsNotNone(self.agent._cache_defer_guard(budget, chars=1000))
        self.assertIsNone(self.agent._cache_defer_guard(budget, chars=2*self.agent._effective_budget()+1))

    def test_expired_within_run_observation_cannot_defer_compaction(self):
        self.first()
        self.agent._cache_evidence['observed_at'] -= 61
        budget = {'input_budget': 100000, 'estimated_input_tokens': 1000}
        self.assertIsNone(self.agent._cache_defer_guard(budget, chars=1000))

    def test_fallback_receipt_does_not_warm_the_primary_route(self):
        class Transport:
            def complete(inner, provider, model, messages, **options):
                if provider.name == 'p':
                    raise TransportError('fixture primary unavailable')
                return 'answer', Usage(100, 10, model, provider.name, cached_tokens=95, cache_observed=True)
        fallback = Provider('fallback', 'https://fallback.invalid', default_model='mimo-v2.6-pro')
        self.agent.router = ModelRouter([self.provider, fallback], transport=Transport(),
            chain=[('p', self.provider.default_model), ('fallback', fallback.default_model)], retries_per_provider=0)
        self.first()
        self.assertIsNone(self.agent._recent_cache_rate)
        self.assertIsNone(self.agent._cache_evidence)

    def test_unknown_invalid_and_aggregate_receipts_cannot_drive_deferral(self):
        for requests in ([{'usage': {'prompt_tokens': 100, 'cached_tokens': 0, 'cache_observed': False}}],
                         [{'usage': {'prompt_tokens': 100, 'cached_tokens': 101}}],
                         [{'stage': 'integrate', 'usage': {'prompt_tokens': 100, 'cached_tokens': 90}}],
                         [{'usage': {'prompt_tokens': 100, 'cached_tokens': 90}}]*2, [None]):
            completion = Completion('ok', Usage(), requests=requests)
            self.assertIsNone(foreground_hit_rate(completion))
        self.assertIsNone(foreground_hit_rate(Completion('one MoA candidate',
            Usage(100, cached_tokens=90, cache_observed=True), aggregated=True)))

    def test_host_suffix_trim_does_not_accept_edited_or_fabricated_history(self):
        prefix = ConversationPrefix()
        history = [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'answer'}]
        prefix.commit(history=history, messages=history, guard='host', rule_context='', rule_sources=[], targets=[])
        self.assertFalse(prefix.trim_history(history, [{'role': 'assistant', 'content': 'edited'}]))
        self.assertFalse(prefix.trim_history([history[-1]], [history[-1]]))
        self.assertIsNotNone(prefix.candidate(history, 'host'))
        self.assertTrue(prefix.trim_history(history, [history[-1]]))
        self.assertIsNotNone(prefix.candidate([history[-1]], 'host'))
        self.assertIsNone(prefix.candidate(history, 'host'))


if __name__ == '__main__':
    unittest.main()
