"""Work 工具回合上限：临近上限的临时提醒、触顶后的进度小结与失败回退。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from .conversation import BUDGET_SUMMARY, MAX_TURNS, budget_lead, run, tool_schemas, turn_limit
from keystone.loop_limits import NO_STEP_LIMIT
from keystone_client import ChatMessage, CompletionResult, GatewayError


class Client:
    def __init__(self, summary='已读 8 个文件，还剩汇总。', fail_summary=False):
        self.requests, self.summary, self.fail_summary = [], summary, fail_summary

    def list_tools(self, **_):
        return [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]

    def stream_chat(self, messages, on_chunk, **_):
        self.requests.append(list(messages))
        if messages[-1].content == BUDGET_SUMMARY:
            if self.fail_summary:
                raise GatewayError('upstream down')
            on_chunk(self.summary)
            return CompletionResult(self.summary)
        n = len(self.requests)
        return CompletionResult('', tool_calls=[{'id': f'c{n}', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path": "f%d.txt"}' % n}}])

    def call_tool(self, name, args):
        return {'ok': True, 'content': 'value of ' + args['path']}


def drive(client, **options):
    chunks, checkpoints = [], []
    result = run(client, [ChatMessage('user', 'read everything')], model='test', effort=None,
                 cancel=threading.Event(), authorize=lambda *_: None, chunk=chunks.append,
                 checkpoint=checkpoints.append, status=lambda _: None, next_round=lambda: None, **options)
    return result, chunks, checkpoints


class TurnBudgetTests(unittest.TestCase):
    def test_changing_work_context_follows_shared_history_and_stays_out_of_checkpoints(self):
        old = Client()
        _, _, history = drive(old, system_context='fixed', turn_context='observation A', max_turns=1)
        start, summary = old.requests
        self.assertEqual(start[0], summary[0])
        self.assertEqual(summary[:len(start)], start)
        self.assertEqual([m.role for m in summary[-3:]], ['assistant', 'tool', 'user'])
        self.assertEqual(sum('observation A' in m.content for m in summary), 1)
        self.assertFalse(any('observation A' in m.content for m in history[-1]))
        second = Client()
        messages = [*history[-1], ChatMessage('assistant', 'first answer'), ChatMessage('user', 'follow-up')]
        run(second, messages, model='test', effort=None, cancel=threading.Event(),
            authorize=lambda *_: None, chunk=lambda _: None, checkpoint=lambda _: None,
            status=lambda _: None, next_round=lambda: None, system_context='fixed', turn_context='observation B',
            max_turns=1)
        current = second.requests[0]
        self.assertEqual(current[:2], start[:2])
        self.assertIn('observation B', current[-1].content)
        self.assertFalse(any('observation A' in m.content for m in current))

    def test_tool_declarations_have_stable_order_and_keep_local_overrides(self):
        gateway = [{'type': 'function', 'function': {'name': name}} for name in ('zeta', 'alpha')]
        replacement = {'type': 'function', 'function': {'name': 'alpha', 'description': 'local'}}
        local = {'alpha': (replacement, None)}
        self.assertEqual(tool_schemas(gateway, local), [replacement, gateway[0]])
        self.assertEqual(tool_schemas(list(reversed(gateway)), local), [replacement, gateway[0]])

    def test_reminders_stay_off_history_and_summary_marks_the_reply_continuable(self):
        client = Client()
        result, chunks, checkpoints = drive(client)
        self.assertEqual(len(client.requests), MAX_TURNS + 1)
        tails = [request[-1].content for request in client.requests[:MAX_TURNS]]
        self.assertFalse(any('本轮' in tail for tail in tails[:-2]))
        self.assertIn('还剩 2 次', tails[-2])
        self.assertIn('最后一次', tails[-1])
        self.assertTrue(all('本轮' not in m.content for m in checkpoints[-1]))
        self.assertTrue(result.budget_exhausted)
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(result.text, budget_lead(MAX_TURNS) + client.summary)
        self.assertEqual(client.requests[-1][0].content, client.requests[-2][0].content)
        self.assertEqual(''.join(chunks), result.text)

    def test_failed_or_tool_only_summary_falls_back_to_the_limit_error(self):
        for client in (Client(fail_summary=True), Client(summary='')):
            with self.subTest(fail=client.fail_summary), self.assertRaisesRegex(GatewayError, '轮工具调用上限'):
                drive(client)

    def test_turn_limit_follows_keystone_loop_config(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(turn_limit(folder), NO_STEP_LIMIT)  # the framework default is no limit
            Path(folder, 'keystone.patch.json').write_text(
                json.dumps([{'id': 'loop', 'config': {'maxSteps': 7}}]), encoding='utf-8')
            self.assertEqual(turn_limit(folder), 7)
        client = Client()
        result, _, _ = drive(client, max_turns=7)
        self.assertEqual(len(client.requests), 8)
        self.assertIn('还剩 2 次', client.requests[5][-1].content)
        self.assertTrue(result.text.startswith(budget_lead(7)))
        small = Client()
        drive(small, max_turns=4)
        self.assertFalse(any('本轮' in r[-1].content for r in small.requests[:4]))


if __name__ == '__main__':
    unittest.main()
