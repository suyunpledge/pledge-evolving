"""编辑器连续对话的最终 HTTP 请求体对照：只用本地响应替身。"""
import io
import copy
import json
import unittest

import test_development_execution as fixtures
from forge.model import HttpTransport, ModelRouter, Provider
from forge.compaction import ContextBudget
from forge.context_transaction import valid_tool_pairs


class EditorWirePrefixTests(unittest.TestCase):
    def setUp(self):
        self.transport_complete = HttpTransport.complete
        fixtures.ExecutionTests.setUp(self)
    runtime = fixtures.ExecutionTests.runtime

    def test_editor_history_count_rollover_keeps_private_request_snapshot(self):
        router = fixtures.Router(['answer', 'next answer'])
        runtime = self.runtime(router=router)
        runtime.bridge.history = [{'role': 'assistant', 'content': f'old {i}'} for i in range(200)]
        first = runtime.run('first', permission='read-only', planning='none')
        self.assertTrue(first['ok'], first)
        self.assertEqual(len(runtime.bridge.history), 200)
        sent = copy.deepcopy(router.calls[-1][0])
        second = runtime.run('second', permission='read-only', planning='none')
        self.assertTrue(second['ok'], second)
        self.assertEqual(len(runtime.bridge.history), 200)
        self.assertEqual(sent, router.calls[-1][0][:len(sent)])
        self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in runtime.bridge.agent.events))

    def test_work_and_code_continue_compressed_actual_http_prefix_and_revision(self):
        for mode in ('general', 'development'):
            with self.subTest(mode=mode):
                calls = []
                def opener(request, **kwargs):
                    calls.append(json.loads(request.data))
                    return io.BytesIO(json.dumps({'choices': [{'message': {
                        'role': 'assistant', 'content': 'verified answer', 'reasoning_content': 'complete thought'},
                        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 30000, 'completion_tokens': 20,
                        'prompt_tokens_details': {'cached_tokens': 28000}}}).encode())
                transport = HttpTransport(response_opener=opener)
                transport.complete = self.transport_complete.__get__(transport, HttpTransport)
                router = ModelRouter([Provider('fixture', 'http://127.0.0.1:1', default_model='mimo-v2.6-pro',
                    route_capabilities={'contextWindow': 200000, 'maxOutputTokens': 4096})],
                    transport=transport, retries_per_provider=0)
                runtime = self.runtime(name=mode, router=router)
                runtime.state.switch(mode)
                agent = runtime.bridge.agent
                agent.limits.context_chars = 20000
                agent.compactor = ContextBudget(max_chars=20000, keep_tail=6)
                runtime.bridge.history = [{'role': 'assistant', 'content': f'old record {i}: ' + 'data ' * 900}
                                         for i in range(20)]
                first = runtime.run('inspect the latest task', permission='read-only', planning='none')
                self.assertTrue(first['ok'], first)
                self.assertTrue(any(e['type'] == 'compaction' and e.get('committed') for e in agent.events))
                revision = first['usage']['requests'][0]['context_revision']
                self.assertGreater(revision, 0)
                second = runtime.run('continue with an explanation', permission='read-only', planning='none')
                self.assertTrue(second['ok'], second)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0]['messages'], calls[1]['messages'][:len(calls[0]['messages'])])
                self.assertTrue(all(valid_tool_pairs(c['messages']) for c in calls))
                self.assertEqual(second['usage']['requests'][0]['context_revision'], revision)
                self.assertNotIn('cache_rebuild', second['usage']['requests'][0])
                self.assertTrue(any(e['type'] == 'cache_observation_reused' for e in agent.events))

    def test_editor_followup_preserves_actual_deepseek_wire_prefix(self):
        calls = []
        def opener(request, **kwargs):
            calls.append(json.loads(request.data))
            if len(calls) == 1:
                message = {'role': 'assistant', 'content': '', 'reasoning_content': 'fixture reasoning',
                    'tool_calls': [{'id': 'read-1', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"value.py"}'}}]}
                finish = 'tool_calls'
            else:
                message = {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'final reasoning'}
                finish = 'stop'
            return io.BytesIO(json.dumps({'choices': [{'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 3000, 'completion_tokens': 10}}).encode())
        transport = HttpTransport(response_opener=opener)
        transport.complete = self.transport_complete.__get__(transport, HttpTransport)
        router = ModelRouter([Provider('fixture', 'http://127.0.0.1:1', default_model='deepseek-flash')],
            transport=transport, retries_per_provider=0)
        runtime = self.runtime(router=router)
        first = runtime.run('read the code', permission='read-only', planning='none')
        self.assertTrue(first['ok'])
        self.assertEqual(len(calls), 2)
        history = copy.deepcopy(runtime.bridge.history)
        second = runtime.run('explain that code', permission='read-only', planning='none')
        self.assertTrue(second['ok'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]['tools'], calls[2]['tools'])
        self.assertEqual(calls[1]['messages'], calls[2]['messages'][:len(calls[1]['messages'])])
        self.assertIn('fixture reasoning', str(calls[2]['messages']))
        self.assertIn('final reasoning', str(calls[2]['messages']))
        self.assertIsNone(second['usage']['cache_hit_rate'])
        usage_event = next(e for e in runtime.bridge.agent.events if e['type'] == 'usage')
        self.assertIsNone(usage_event['cache_hit_rate'])
        self.assertTrue(any(e['type'] == 'cache_prefix_reused' for e in runtime.bridge.agent.events))
        # Fixture 没有厂商缓存回执：不得报告任何“实测命中提升”。
        self.assertEqual(second['usage']['cache_trajectory']['coverage']['all_observed']['requests'], 0)
        # 同一宿主历史禁用快照，直接对照原先的“简化历史重建”路径。
        runtime.bridge.history = history
        runtime.bridge.agent._conversation_prefix.clear()
        rebuilt = runtime.run('explain that code', permission='read-only', planning='none')
        self.assertTrue(rebuilt['ok'])
        def common(left, right):
            count = 0
            for a, b in zip(left, right):
                if a != b:
                    break
                count += 1
            return count
        retained = common(calls[1]['messages'], calls[2]['messages'])
        baseline = common(calls[1]['messages'], calls[3]['messages'])
        self.assertGreater(retained, baseline)
        print(json.dumps({'retained_prefix_messages': retained, 'baseline_prefix_messages': baseline}))


if __name__ == '__main__':
    unittest.main()
