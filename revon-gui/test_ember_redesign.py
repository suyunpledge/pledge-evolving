"""Ember 设计稿落地的纯逻辑回归：缓存命中率、命令面板排序、图标 path 解析、问候归并。"""
import unittest

import revon_gui_v2 as gui
import gui_theme as theme
from command_palette import PaletteItem, rank, score
from test_ui_ergonomics import contrast
from ui_icons import ICONS, PALETTE, _path, _shapes


class CacheHitRateTests(unittest.TestCase):
    rate = staticmethod(gui.RevonGuiApp._cache_hit_rate)

    def test_reported_rate_is_used_and_clamped(self):
        self.assertAlmostEqual(self.rate({"cache_hit_rate": 0.87}), 0.87)
        self.assertAlmostEqual(self.rate({"cache_hit_rate": 87}), 0.87)

    def test_openai_and_anthropic_usage_shapes(self):
        self.assertAlmostEqual(self.rate({"prompt_tokens": 1000, "cached_tokens": 900}), 0.9)
        self.assertAlmostEqual(self.rate({"prompt_tokens": 400,
                                          "prompt_tokens_details": {"cached_tokens": 100}}), 0.25)
        self.assertAlmostEqual(self.rate({"input_tokens": 100, "cache_read_input_tokens": 800,
                                          "cache_creation_input_tokens": 100}), 0.8)

    def test_missing_data_never_invents_a_rate(self):
        for usage in (None, {}, {"total_tokens": 50}, {"prompt_tokens": 0, "cached_tokens": 0},
                      {"cache_hit_rate": True}, {"prompt_tokens": "x", "cached_tokens": 1}):
            self.assertIsNone(self.rate(usage), usage)


class PaletteRankTests(unittest.TestCase):
    def setUp(self):
        noop = lambda: None  # noqa: E731
        self.items = [PaletteItem("新建对话", "命令", noop, keywords="new chat"),
                      PaletteItem("配置 API 与模型", "命令", noop, keywords="model provider"),
                      PaletteItem("模型对比讨论", "会话", noop)]

    def test_empty_query_keeps_group_order(self):
        results, scope, _query = rank(self.items, "")
        self.assertIsNone(scope)
        self.assertEqual([item.group for item in results], ["命令", "命令", "会话"])

    def test_scope_prefix_limits_groups(self):
        results, scope, query = rank(self.items, "#模型")
        self.assertEqual((scope, query), ("会话", "模型"))
        self.assertEqual([item.label for item in results], ["模型对比讨论"])
        results, scope, _query = rank(self.items, ">model")
        self.assertEqual([item.label for item in results], ["配置 API 与模型"])

    def test_subsequence_match_and_rejection(self):
        self.assertIsNotNone(score("xjdh", "新建对话 xin jian dui hua"))
        self.assertIsNone(score("zzz", "新建对话"))
        self.assertGreater(score("新建", "新建对话"), score("新话", "新建对话"))


class IconPathTests(unittest.TestCase):
    def test_closed_and_open_subpaths(self):
        shapes = _path("M4 4h16v16H4z M2 2l4 4")
        self.assertEqual([kind for kind, _ in shapes], ["poly", "line"])
        self.assertEqual(shapes[0][1][:4], (4, 4, 20, 4))

    def test_arc_reaches_its_endpoint_inside_the_grid(self):
        (_kind, points), = _path("M4.5 20.5a7.5 7.5 0 0 1 15 0")
        self.assertEqual(points[-2:], (19.5, 20.5))
        self.assertTrue(all(12.9 <= y <= 20.6 for y in points[1::2]))

    def test_filled_specs_and_flip(self):
        self.assertEqual([k for k, _ in _shapes("R 1 1 4 4 1", "C 12 12 2")], ["fill", "dot"])
        up, down = ICONS["thumb_up"], ICONS["thumb_down"]
        self.assertEqual(up[0][1][1], 24 - down[0][1][1])

    def test_every_icon_stays_on_the_24_grid(self):
        for name, shapes in ICONS.items():
            for _kind, coords in shapes:
                self.assertTrue(all(-0.5 <= value <= 24.5 for value in coords), name)


class EmberTokenTests(unittest.TestCase):
    def test_on_accent_ink_and_atlas_palette(self):
        C = theme.C
        for fill in ("accent", "accent_hover", "accent_pressed"):
            self.assertGreaterEqual(contrast(C["on_accent"], C[fill]), 4.5, fill)
        self.assertGreaterEqual(contrast(C["ember"], C["chat"]), 4.5)
        self.assertEqual(PALETTE["accent"], C["ember"])
        self.assertEqual(PALETTE["ink"], C["on_accent"])


class WorkIdentityTests(unittest.TestCase):
    def test_system_prompt_is_a_stable_prefix_outside_history(self):
        import threading
        from http.server import HTTPServer
        import test_revon_client as tfc
        from revon_client import ChatMessage, RevonGatewayClient
        seen = []

        class Capture(tfc.MockHandler):
            def _do_stream(self, body):
                seen.append(body)
                return super()._do_stream(body)

        server = HTTPServer(("127.0.0.1", 0), Capture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = RevonGatewayClient(f"http://127.0.0.1:{server.server_address[1]}")
            history = [ChatMessage("user", "你可以做些什么")]
            client.stream_chat(history, model="y", system=gui.WORK_SYSTEM_PROMPT)
            client.stream_chat(history, model="y")
        finally:
            server.shutdown()
            server.server_close()
        with_system, plain = seen
        self.assertEqual(with_system["messages"][0],
                         {"role": "system", "content": gui.WORK_SYSTEM_PROMPT})
        self.assertEqual(with_system["messages"][1:], plain["messages"])
        self.assertEqual(len(history), 1)
        self.assertIn("revon", gui.WORK_SYSTEM_PROMPT)


class GreetingFoldTests(unittest.TestCase):
    def test_only_short_greeting_sessions_fold(self):
        greet = gui.RevonGuiApp._is_greeting_session
        self.assertTrue(greet({"title": "你好", "messages": [{}, {}]}))
        self.assertTrue(greet({"title": "Hello!", "messages": []}))
        self.assertFalse(greet({"title": "你好", "messages": [{}] * 6}))
        self.assertFalse(greet({"title": "统一按钮样式", "messages": [{}]}))

    def test_snippet_skips_markdown_noise(self):
        snippet = gui.RevonGuiApp._session_snippet(
            {"messages": [{"role": "user", "content": "hi"},
                          {"role": "assistant", "content": "\n## 结论：可以合并\n细节"}]})
        self.assertEqual(snippet, "结论：可以合并")


if __name__ == "__main__":
    unittest.main()
