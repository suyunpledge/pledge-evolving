"""Work Mode 对话的前缀缓存：每轮不同的背景块只能落在请求尾部。

知识库提示曾被插在整段历史之前，且按本轮问题检索、每轮不同——导致每发
一条消息，整段历史都失配、全价重算。真实 _do_send 路径 + stub 客户端。
"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from forge_gui_v2 import insert_turn_context


class InsertTurnContextTests(unittest.TestCase):
    def test_block_goes_right_before_the_latest_question(self):
        messages = ["h1", "h2", "q"]
        insert_turn_context(messages, "kb")
        insert_turn_context(messages, "mem")
        self.assertEqual(messages, ["h1", "h2", "kb", "mem", "q"])
        self.assertEqual(insert_turn_context(["q"], "kb"), ["kb", "q"])


class WorkModePrefixTests(unittest.TestCase):
    def test_second_turn_request_starts_with_first_turn_history(self):
        from test_layout_dpi import isolated_app, pump
        seen = []

        class Writer:
            base_url = "http://127.0.0.1:12345"
            def health(self, **kwargs): return True, "ok"
            def list_tools(self): return []
            def stream_chat(self, messages, **kwargs):
                seen.append([(m.role, m.content) for m in messages])
                kwargs["on_chunk"]("回答")
                return SimpleNamespace(text="回答", tool_calls=[])

        lessons = iter(["教训 A：先看日志", "教训 B：别改公共接口"])

        def send(app, root, text):
            with patch.object(app, "_plan_sidecars", return_value=None), \
                 patch.object(app, "_reload_plugin_tools", return_value=([], None)):
                app.send_var.set(text)
                app._do_send()
                deadline = time.monotonic() + 5
                while app._sending and time.monotonic() < deadline:
                    pump(root, .03)

        with isolated_app() as (root, app, errors):
            app.client = Writer()
            app.knowledge = SimpleNamespace(context_for=lambda prompt: next(lessons))
            send(app, root, "第一个问题")
            send(app, root, "第二个问题")
            first, second = seen[-2], seen[-1]
            self.assertTrue(first[-2][1].startswith("[知识库提示]"))
            self.assertEqual(first[-1], ("user", "第一个问题"))
            self.assertTrue(second[-2][1].startswith("[知识库提示]"))
            self.assertIn("教训 B", second[-2][1])
            self.assertEqual(second[-1], ("user", "第二个问题"))
            # 第二轮以第一轮的历史开头：首轮背景块不进历史，历史前缀可整段命中缓存。
            history = [m for m in first if not m[1].startswith("[知识库提示]")]
            self.assertEqual(second[:len(history)], history)
            self.assertFalse(errors)


if __name__ == "__main__":
    unittest.main()
