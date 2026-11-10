"""DSH 风格跨轮前缀延续：实际 Agent 请求录制，不伪造厂商命中率。"""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .conversation_prefix import ConversationPrefix
from .loop import Agent, LoopLimits
from .model import Completion, Usage, ModelRouter, Provider
from .policy import Mode, Policy
from .protocol import _cache_trajectory
from .test_project_rules import Router
from .tools import build_builtin_registry, ToolSpec, ToolResult


def call(name, **args):
    return json.dumps({'tool': name, 'args': args})


class ConversationPrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'project'
        self.ws.mkdir()
        (self.ws / 'a.txt').write_text('file content', encoding='utf-8')
        self.router = Router([])
        self.agent = Agent(home=self.root / 'home', workspace=self.ws, router=self.router,
            registry=build_builtin_registry(), policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS),
            limits=LoopLimits(max_steps=8, context_chars=100000), project_rules=True)
        self.addCleanup(self.agent.secret_scope.close)

    def chat(self, answers, task='first', history=None):
        self.router.answers = iter(answers)
        return self.agent.run(task, history=history)

    def history(self, task, report):
        return [{'role': 'user', 'content': task}, {'role': 'assistant', 'content': report.text}]

    def test_continuation_keeps_entire_previous_request_including_tools(self):
        first = self.chat([call('read_file', path='a.txt'), 'answer'])
        sent = copy.deepcopy(self.router.calls[-1])
        second = self.chat(['next answer'], task='second', history=self.history('first', first))
        current = self.router.calls[-1]
        self.assertEqual(current[:len(sent)], sent)
        self.assertIn('file content', str(current))
        self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in second.events))
        self.assertTrue(current[-1]['content'].startswith('second'))

    def test_three_consecutive_turns_keep_full_sent_prefix(self):
        history = []
        previous = None
        for index in range(3):
            task = f'turn {index}'
            report = self.chat([f'answer {index}'], task=task, history=history)
            current = self.router.calls[-1]
            if previous is not None:
                self.assertEqual(previous, current[:len(previous)])
            history.extend(self.history(task, report))
            previous = copy.deepcopy(current)

    def test_unchanged_development_guidance_is_not_repeated_in_followup(self):
        from .development.runtime import DEVELOPMENT_GUIDANCE
        self.agent.extra_system = DEVELOPMENT_GUIDANCE
        first = self.chat(['answer'])
        sent = copy.deepcopy(self.router.calls[-1])
        envelope = self.agent.run_envelope()
        report = self.chat(['next'], task='second', history=self.history('first', first))
        messages = self.router.calls[-1]
        self.assertEqual(messages[:len(sent)], sent)
        self.assertEqual(sum(m.get('content') == envelope for m in messages), 1)
        self.assertTrue(any(e['type'] == 'run_envelope_reused' for e in report.events))

    def test_guidance_changes_append_even_when_an_older_version_matches(self):
        history = []
        for index, guidance in enumerate(('guidance A', 'guidance B', 'guidance A')):
            self.agent.extra_system = guidance
            task = f'turn {index}'
            report = self.chat(['answer'], task=task, history=history)
            history += self.history(task, report)
            self.assertFalse(any(e['type'] == 'run_envelope_reused' for e in report.events))
        self.assertEqual(sum(m.get('content') == self.agent.run_envelope()
                             for m in self.router.calls[-1]), 2)

    def test_compaction_removed_envelope_is_reinserted(self):
        first = self.chat(['answer'])
        envelope = self.agent.run_envelope()
        saved = self.agent._conversation_prefix._saved
        saved['messages'] = [m for m in saved['messages'] if m.get('content') != envelope]
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertTrue(any(m.get('content') == envelope for m in self.router.calls[-1]))
        self.assertFalse(any(e['type'] == 'run_envelope_reused' for e in report.events))

    def test_untrusted_chat_lookalike_cannot_suppress_host_envelope(self):
        envelope = self.agent.run_envelope()
        report = self.chat(['answer'], history=[{'role': 'user', 'content': envelope}])
        self.assertEqual(sum(m.get('content') == envelope for m in self.router.calls[-1]), 2)
        self.assertFalse(any(e['type'] == 'run_envelope_reused' for e in report.events))

    def test_native_pairs_and_reasoning_survive_followup_for_both_wires(self):
        for wire in ('openai', 'anthropic'):
            self.agent.wire_hint = wire
            response = Completion('', Usage(1, 1, 'fake', 'fake'), wire=wire,
                tool_calls=[{'id': 'read-1', 'name': 'read_file', 'args': {'path': 'a.txt'}, 'wire': wire}])
            original = ({'role': 'assistant', 'content': 'answer', 'reasoning_content': 'final thought'}
                if wire == 'openai' else {'role': 'assistant', 'content': [
                    {'type': 'thinking', 'thinking': 'final thought', 'signature': 'fixture signature'},
                    {'type': 'text', 'text': 'answer'}]})
            final = Completion('answer', Usage(1, 1, 'fake', 'fake'), wire=wire, assistant_message=original)
            first = self.chat([response, final])
            sent = copy.deepcopy(self.router.calls[-1])
            report = self.chat(['next'], task='second', history=self.history('first', first))
            self.assertEqual(self.router.calls[-1][:len(sent)], sent)
            self.assertIn('read-1', str(self.router.calls[-1]))
            self.assertIn('final thought', str(self.router.calls[-1]))
            self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_edited_truncated_and_unrelated_history_do_not_reuse(self):
        first = self.chat([call('read_file', path='a.txt'), 'answer'])
        for history in [[{'role': 'user', 'content': 'edited'}], self.history('first', first)[1:],
                        [{'role': 'user', 'content': 'unrelated'}, {'role': 'assistant', 'content': 'answer'}]]:
            report = self.chat(['next'], task='second', history=history)
            self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_same_task_without_history_is_an_independent_run(self):
        self.chat([call('read_file', path='a.txt'), 'answer'])
        report = self.chat(['next'])
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))
        self.assertNotIn('file content', str(self.router.calls[-1]))

    def test_policy_and_tool_changes_invalidate_reuse(self):
        first = self.chat(['answer'])
        self.agent.policy = replace(self.agent.policy, mode=Mode.READ_ONLY)
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))
        self.agent.registry.register(ToolSpec('new_read', '', lambda a, c: ToolResult(True)), trusted=True)
        report = self.chat(['third'], task='third', history=[*self.history('first', first),
            {'role': 'user', 'content': 'second'}, {'role': 'assistant', 'content': report.text}])
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_changed_or_deleted_nested_guidance_rebuilds(self):
        (self.ws / 'src').mkdir()
        rules = self.ws / 'src' / 'CLAUDE.md'
        rules.write_text('old nested guidance', encoding='utf-8')
        (self.ws / 'src' / 'b.txt').write_text('b', encoding='utf-8')
        first = self.chat([call('read_file', path='src/b.txt'), 'answer'])
        rules.write_text('new nested guidance', encoding='utf-8')
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))
        self.assertNotIn('old nested guidance', str(self.router.calls[-1]))

    def test_new_guidance_appends_without_rewriting_previous_prefix(self):
        (self.ws / 'AGENTS.md').write_text('root guidance', encoding='utf-8')
        first = self.chat(['answer'])
        sent = copy.deepcopy(self.router.calls[-1])
        (self.ws / 'CLAUDE.md').write_text('added guidance', encoding='utf-8')
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertEqual(self.router.calls[-1][:len(sent)], sent)
        self.assertIn('added guidance', str(self.router.calls[-1]))
        self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_failed_run_clears_previous_conversation_snapshot(self):
        first = self.chat(['answer'])
        self.agent.limits.max_steps = 1
        stopped = self.chat([call('read_file', path='a.txt')], task='second', history=self.history('first', first))
        self.assertEqual(stopped.stopped, 'max_steps')
        report = self.chat(['next'], task='third', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_deleted_guidance_invalidates_old_rule_context(self):
        rules = self.ws / 'AGENTS.md'
        rules.write_text('deleted guidance', encoding='utf-8')
        first = self.chat(['answer'])
        rules.unlink()
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))
        self.assertNotIn('deleted guidance', str(self.router.calls[-1]))

    def test_identity_head_change_invalidates_snapshot(self):
        first = self.chat(['answer'])
        self.agent.name = 'new identity'
        report = self.chat(['next'], task='second', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))
        self.assertIn('new identity', self.router.calls[-1][0]['content'])

    def test_route_endpoint_change_invalidates_snapshot(self):
        from .test_prefix_cache import _Recorder
        transport = _Recorder()
        provider = Provider('fixture', 'http://before.invalid', default_model='deepseek-flash')
        self.agent.router = ModelRouter([provider], transport=transport, retries_per_provider=0)
        first = self.agent.run('first')
        provider.base_url = 'http://after.invalid'
        report = self.agent.run('second', history=self.history('first', first))
        self.assertFalse(any(e['type'] == 'cache_prefix_reused' for e in report.events))

    def test_run_hit_rate_weights_only_observed_input_tokens(self):
        rows = [{'usage': {'prompt_tokens': prompt, 'cached_tokens': cached, 'cache_observed': observed}}
                for prompt, cached, observed in [(100, 90, True), (900, 90, True), (1000, 0, False)]]
        response = Completion('answer', Usage(2000, 1, 'fake', 'fake', cached_tokens=180), requests=rows)
        report = self.chat([response])
        self.assertEqual(report.usage['cache_hit_rate'], .18)
        event = next(e for e in report.events if e['type'] == 'usage')
        self.assertEqual(event['cache_hit_rate'], .18)
        self.assertEqual(event['cache_observed_requests'], 2)
        self.assertEqual(event['cache_unknown_requests'], 1)

    def test_snapshot_size_and_broken_tool_groups_fail_closed(self):
        cache = ConversationPrefix()
        kwargs = dict(history=[{'role': 'user', 'content': 'x'}], guard='guard',
                      rule_context='', rule_sources=[], targets=[])
        self.assertFalse(cache.commit(messages=[{'role': 'tool', 'tool_call_id': 'orphan'}], **kwargs))
        with patch('forge.conversation_prefix.MAX_SNAPSHOT_BYTES', 20):
            self.assertFalse(cache.commit(messages=[{'role': 'user', 'content': 'long'}], **kwargs))

    def test_snapshot_detaches_external_mutations(self):
        cache = ConversationPrefix()
        history = [{'role': 'user', 'content': 'x'}]
        messages = [{'role': 'user', 'content': 'x'}]
        cache.commit(history=history, messages=messages, guard='g', rule_context='', rule_sources=[], targets=[])
        messages[0]['content'] = 'mutated'
        first = cache.candidate(history, 'g')
        first['messages'][0]['content'] = 'also mutated'
        self.assertEqual(cache.candidate(history, 'g')['messages'][0]['content'], 'x')


class HonestCacheMetricsTests(unittest.TestCase):
    def row(self, cached, observed=True):
        return {'usage': {'prompt_tokens': 100, 'cached_tokens': cached, 'cache_observed': observed}}

    def test_unknown_cache_usage_is_not_a_cold_miss(self):
        result = _cache_trajectory({'requests': [self.row(0, False)]})
        self.assertNotIn('hit_rate_by_request', result)
        self.assertEqual(result['coverage']['unknown_requests'], 1)

    def test_unknown_gap_does_not_invent_a_bust(self):
        result = _cache_trajectory({'requests': [self.row(90), self.row(0, False), self.row(10)]})
        self.assertEqual(result['hit_rate_by_request'], [.9, .1])
        self.assertEqual(result['observed_request_indices'], [0, 2])
        self.assertEqual(result['busts'], [])

    def test_unknown_first_request_has_unknown_cold_start(self):
        result = _cache_trajectory({'requests': [self.row(0, False), self.row(10)]})
        self.assertIsNone(result['cold_start'])
        self.assertEqual(result['coverage']['all_observed']['hit_rate'], .1)


if __name__ == '__main__':
    unittest.main()
