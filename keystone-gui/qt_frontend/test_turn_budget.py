"""Work 工具回合上限：临近上限的临时提醒、触顶后的进度小结与失败回退。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from .conversation import BUDGET_SUMMARY, MAX_TURNS, budget_lead, run, turn_limit
from keystone_client import ChatMessage, CompletionResult, GatewayError


class Client:
    def __init__(self, summary='已读 8 个文件，还剩汇总。', fail_summary=False):
        self.requests, self.summary, self.fail_summary = [], summary, fail_summary

    def list_tools(self, **_):
        return [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]

    def stream_chat(self, messages, on_chunk, **_):
        self.requests.append(list(messages))
        if BUDGET_SUMMARY in messages[0].content:
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
        self.assertEqual(''.join(chunks), result.text)

    def test_failed_or_tool_only_summary_falls_back_to_the_limit_error(self):
        for client in (Client(fail_summary=True), Client(summary='')):
            with self.subTest(fail=client.fail_summary), self.assertRaisesRegex(GatewayError, '轮工具调用上限'):
                drive(client)

    def test_turn_limit_follows_keystone_loop_config(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(turn_limit(folder), MAX_TURNS)
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
