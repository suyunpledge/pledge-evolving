"""Sending must preserve visible messages after delayed stream completion."""
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chat_widgets as cw
from test_layout_dpi import isolated_app, pump, descendants


class ScrollGeometryTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.original_scaling = self.root.tk.call("tk", "scaling")
        self.root.attributes("-alpha", 0)
        self.root.geometry("600x400")
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        self.root.tk.call("tk", "scaling", self.original_scaling)
        self.root.destroy()

    def test_shrink_at_five_scales_and_resize_keeps_content_visible(self):
        for scale in (1, 1.25, 1.5, 1.75, 2):
            with self.subTest(scale=scale):
                self.root.tk.call("tk", "scaling", scale * 96 / 72)
                scroll = cw.ScrollArea(self.root, pady=18)
                scroll.pack(fill=tk.BOTH, expand=True)
                card = tk.Frame(scroll.inner)
                card.pack(fill=tk.X)
                content = tk.Frame(card, height=2600)
                content.pack(fill=tk.X)
                scroll.scroll_to_end()
                pump(self.root, .05)
                content.configure(height=300)
                pump(self.root, .1)
                for size in ("600x400", "350x240", "900x600"):
                    self.root.geometry(size)
                    pump(self.root, .05)
                    region = tuple(map(float, self.root.tk.splitlist(scroll.canvas.cget("scrollregion"))))
                    self.assertEqual(region[3], scroll.canvas.bbox("all")[3])
                    self.assertGreater(content.winfo_rooty() + content.winfo_height(), scroll.canvas.winfo_rooty())
                scroll.destroy()
        self.assertEqual(self.errors, [])

    def test_geometry_sync_preserves_manual_scroll_and_ignores_siblings(self):
        scroll = cw.ScrollArea(self.root)
        scroll.pack(fill=tk.BOTH, expand=True)
        card = tk.Frame(scroll.inner)
        card.pack(fill=tk.X)
        content = tk.Frame(card, height=2600)
        content.pack(fill=tk.X)
        scroll.scroll_to_end()
        pump(self.root, .1)
        scroll._manual_scroll("moveto", .1)
        content.configure(height=1600)
        pump(self.root, .1)
        self.assertFalse(scroll._scroll_follow_end)
        self.assertLess(scroll.canvas.yview()[1], .8)
        region = tuple(map(float, self.root.tk.splitlist(scroll.canvas.cget("scrollregion"))))
        self.assertEqual(region[3], scroll.canvas.bbox("all")[3])
        scroll._content_layout_changed(SimpleNamespace(widget=self.root))
        self.assertIsNone(scroll._layout_job)
        self.assertEqual(self.errors, [])

    def test_destroy_cancels_layout_job_and_removes_only_own_binding(self):
        self.root.bind("<Configure>", lambda _: None, add="+")
        before = self.root.bind("<Configure>")
        scroll = cw.ScrollArea(self.root)
        content = tk.Frame(scroll.inner)
        scroll._content_layout_changed(SimpleNamespace(widget=content))
        token = scroll._layout_job
        self.assertIsNotNone(token)
        scroll.destroy()
        self.assertEqual(self.root.bind("<Configure>").strip(), before.strip())
        self.assertNotIn(token, self.root.tk.call("after", "info"))
        pump(self.root, .05)
        self.assertEqual(self.errors, [])


class MessageDeliveryTests(unittest.TestCase):
    def test_offscreen_content_shrink_updates_scrollregion(self):
        with isolated_app(size=(1360, 860)) as (root, app, errors):
            scroll = app.chat_area.scroll
            app.chat_area.clear()
            card = tk.Frame(scroll.inner)
            card.pack(fill=tk.X)
            content = tk.Frame(card, height=2600)
            content.pack(fill=tk.X)
            scroll.scroll_to_end()
            pump(root, .15)
            self.assertGreater(scroll.canvas.bbox("all")[3], 2000)
            content.configure(height=300)
            pump(root, .3)
            bbox = scroll.canvas.bbox("all")
            region = tuple(map(float, scroll.canvas.tk.splitlist(scroll.canvas.cget("scrollregion"))))
            self.assertEqual(errors, [])
            self.assertEqual(region[3], bbox[3])
            self.assertGreater(content.winfo_rooty() + content.winfo_height(), scroll.canvas.winfo_rooty())

    def test_multiline_history_replay_has_no_empty_tail_below_messages(self):
        with isolated_app(size=(1360, 860)) as (root, app, errors):
            area = app.chat_area
            area.add_user("第一轮测试输入，用来验证真实窗口中的消息显示")
            first = area.add_agent(app=app)
            first.render_markdown("第一段测试回复：内容应保持可见。\n\n第二段测试回复：内容应保持可见。\n\n第三段测试回复：内容应保持可见。")
            first.add_actions([{"label": "打开工作区", "command": lambda: None}])
            pump(root, .2)
            area.add_user("第二轮输入")
            last = area.add_agent(app=app)
            last.render_markdown("## 第二轮测试回复\n\n下面是回复正文。\n\n- 第一项应可见\n- 第二项应可见\n\n结束段落应可见。")
            last.add_actions([{"label": "打开工作区", "command": lambda: None}])
            pump(root, 2.4)
            cv, inner = area.scroll.canvas, area.scroll.inner
            self.assertEqual(errors, [])
            bottom = tuple(map(float, cv.tk.splitlist(cv.cget("scrollregion"))))[3]
            self.assertLessEqual(bottom, inner.winfo_reqheight() + 1)
            self.assertGreater(last.winfo_rooty() + last.winfo_height(), cv.winfo_rooty())

    def test_delayed_reply_stays_visible_after_completion(self):
        with isolated_app(size=(1360, 860)) as (root, app, errors):
            class Client:
                base_url = "http://127.0.0.1:12345"
                def health(self, **kwargs):
                    return True, "ok"
                def list_tools(self):
                    return []
                def stream_chat(self, messages, **kwargs):
                    time.sleep(.1)
                    kwargs["on_chunk"]("可见回复")
                    time.sleep(.1)
                    kwargs["on_chunk"](" 😀")
                    return SimpleNamespace(text="可见回复 😀", tool_calls=[])

            app.client = Client()
            with patch.object(app, "_plan_sidecars", return_value=None), \
                 patch.object(app, "_reload_plugin_tools", return_value=([], None)):
                app.send_var.set("显示测试")
                app._do_send()
                deadline = time.monotonic() + 8
                while app._sending and time.monotonic() < deadline:
                    pump(root, .05)
                pump(root, 2.4)  # The completed reply must stay visible afterward.
            self.assertFalse(app._sending)
            self.assertEqual(errors, [])
            self.assertEqual([m.content for m in app._chat_history], ["显示测试", "可见回复 😀"])
            messages = [w for w in app.chat_area.scroll.inner.winfo_children()
                        if isinstance(w, (cw.UserMessage, cw.AgentMessage))]
            self.assertEqual(len(messages), 2)
            for message in messages:
                self.assertTrue(message.winfo_ismapped())
                self.assertGreater(message.winfo_height(), 20)
                if isinstance(message, cw.UserMessage):
                    self.assertEqual(message.label.cget("text"), "显示测试")
                    self.assertTrue(message.label.winfo_ismapped())
                    continue
                texts = [w for w in descendants(message) if isinstance(w, cw.InlineText)]
                self.assertTrue(texts)
                for text in texts:
                    self.assertTrue(text.winfo_ismapped())
                    self.assertIsNotNone(text.bbox("1.0"))
                    viewport = app.chat_area.scroll.canvas
                    self.assertGreater(text.winfo_rooty() + text.winfo_height(), viewport.winfo_rooty())
                    self.assertLess(text.winfo_rooty(), viewport.winfo_rooty() + viewport.winfo_height())
            self.assertEqual(app.chat_area.scroll.canvas.itemcget(app.chat_area.scroll._win, "state"), "")


if __name__ == "__main__":
    unittest.main()
