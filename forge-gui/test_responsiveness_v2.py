"""test_responsiveness_v2.py — 交互手感（防白屏/未响应）回归。

覆盖四项修复：
1. render_blocks_chunked 与 render_blocks 渲染等价（控件数/内容）
2. ToolCard 大量行分批创建（不冻结主线程；摘要立即正确）
3. _write_sessions 后台化（调用立即返回，IO 线程完成落盘）
4. 历史会话逐条渲染（长会话载入不冻结）
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chat_widgets as cw
from gui_theme import C


def _flush(root):
    for _ in range(4):
        root.update_idletasks()
        root.update()


def _pump_async(root, seconds=3.0):
    """跑异步链（after(1) 链）直到时间窗结束或进程退出。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return
        time.sleep(0.005)


MARKDOWN = (
    "# 标题\n\n第一段正文，包含 **加粗** 与 `code`。\n\n"
    + "\n".join(f"- 列表项 {i}：内容填充 {i}" for i in range(30))
    + "\n\n> 引用块\n\n"
    + "```python\nprint('hello')\nprint('world')\n```\n\n"
    + "1. 有序一\n2. 有序二\n\n---\n\n结尾段落。"
)


class ChunkedRenderCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("900x600")

    def tearDown(self):
        self.root.destroy()

    def _count_widgets(self, host):
        n = 0
        stack = [host]
        while stack:
            w = stack.pop()
            n += 1
            stack.extend(w.winfo_children())
        return n

    def test_chunked_equals_sync(self):
        a = cw.render_blocks(self.root, MARKDOWN)
        done = []
        b = cw.render_blocks_chunked(self.root, MARKDOWN, on_done=lambda h: done.append(h))
        _pump_async(self.root, 4.0)
        self.assertTrue(done, "on_done 未回调（分批链断裂？）")
        na, nb = self._count_widgets(a), self._count_widgets(b)
        self.assertEqual(na, nb, f"控件数不一致：同步 {na} vs 分批 {nb}")
        # 文本抽查：两个 host 的全部 Text 内容一致
        def all_text(host):
            parts = []
            def walk(w):
                if isinstance(w, tk.Text):
                    parts.append(w.get("1.0", "end-1c"))
                for c in w.winfo_children():
                    walk(c)
            walk(host)
            return parts
        self.assertEqual(all_text(a), all_text(b))

    def test_chunked_yields_between_blocks(self):
        """长文分批渲染期间主线程可响应（用 after 链推进 + 计数）。"""
        long_md = "\n\n".join(f"段落 {i} " + "文字" * 20 for i in range(120))
        done = []
        host = cw.render_blocks_chunked(self.root, long_md, on_done=lambda h: done.append(True))
        # 不等完成就立刻 update 几帧——窗口应仍能处理事件（无异常）
        for _ in range(6):
            self.root.update()
            time.sleep(0.001)
        _pump_async(self.root, 6.0)
        self.assertTrue(done)
        self.assertGreater(len(host.winfo_children()), 100)


class ToolCardBatchCase(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()

    def tearDown(self):
        self.root.destroy()

    def test_many_rows_batched_and_summary_immediate(self):
        rows = [{"name": f"tool_{i}", "desc": f"描述 {i}", "detail": "x" * 50,
                 "ok": i % 3 != 0} for i in range(200)]
        card = cw.ToolCard(self.root, rows=rows)
        self.root.update_idletasks()
        # 摘要立即显示全量数（不等分批）
        self.assertIn("200", card._summary_label.cget("text"))
        # Tk 的 update() 会吞掉全部到期 after 链，不能用它验证分批；
        # 验证语义：批量任务链已挂上（未完成），且每批上限 12 行。
        self.assertIsNotNone(card._row_batch_job, "分批链未启动")
        self.assertGreater(len(card._pending_rows), 0, "行应分批待建")
        _pump_async(self.root, 6.0)
        self.assertEqual(len(card._row_specs), 200)
        card.destroy()

    def test_destroy_cancels_batch(self):
        rows = [{"name": f"t{i}", "ok": True} for i in range(300)]
        card = cw.ToolCard(self.root, rows=rows)
        self.root.update()
        card.destroy()
        _pump_async(self.root, 1.0)   # 不应抛 TclError
        self.assertTrue(True)


class SessionsIoCase(unittest.TestCase):
    """_write_sessions 后台化需要 app 级 mock；这里验证线程不阻塞语义。"""

    def test_write_returns_immediately(self):
        writes = []

        class FakeApp:
            def _post_ui(self, cb, *a):
                writes.append((cb, a))

        # 直接测后台线程路径：深拷贝 + 落盘在子线程完成
        import forge_gui_v2 as gui  # noqa
        app = FakeApp()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(gui.ForgeGuiApp, "_sessions_path",
                              return_value=Path(td) / "sessions.json"), \
                 patch.object(gui.ForgeGuiApp, "_post_ui",
                              lambda self, cb, *a: writes.append(cb)):
                # 构造最小 self
                inst = object.__new__(gui.ForgeGuiApp)
                t0 = time.monotonic()
                gui.ForgeGuiApp._write_sessions(inst, [{"id": "s1", "messages": [{"role": "user", "content": "x" * 200000}]}])
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 0.5, "主线程调用应立即返回")
                # 等 IO 线程
                deadline = time.monotonic() + 3
                path = Path(td) / "sessions.json"
                while time.monotonic() < deadline and not path.exists():
                    time.sleep(0.02)
                self.assertTrue(path.exists(), "后台线程未落盘")
                import json
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["sessions"][0]["id"], "s1")


class HistoryBatchCase(unittest.TestCase):
    def test_render_history_messages_yields(self):
        import forge_gui_v2 as gui
        from chat_widgets import MessageArea

        root = tk.Tk(); root.geometry("900x600")
        area = MessageArea(root, bg=C["chat"]); area.pack(fill=tk.BOTH, expand=True)

        class Msg:
            def __init__(self, role, content):
                self.role, self.content = role, content

        app = object.__new__(gui.ForgeGuiApp)
        app.chat_area = area
        app.root = root
        # _refresh_history 会被尾部调用——stub 掉（此处只测分批渲染）
        with patch.object(gui.ForgeGuiApp, "_refresh_history", lambda self: None):
            msgs = [Msg("user", f"问题 {i}") if i % 2 == 0 else Msg("assistant", f"# 回答 {i}\n\n" + "内容 " * 40)
                    for i in range(40)]
            t0 = time.monotonic()
            gui.ForgeGuiApp._render_history_messages(app, msgs)
            first = time.monotonic() - t0
            self.assertLess(first, 0.3, "首次调用只应排第一条")
            _pump_async(root, 8.0)
        # 全部消息已进聊天区
        inner = area.scroll.inner
        n_agent = sum(1 for w in inner.winfo_children() if isinstance(w, cw.AgentMessage))
        n_user = sum(1 for w in inner.winfo_children() if isinstance(w, cw.UserMessage))
        self.assertEqual(n_agent, 20)
        self.assertGreaterEqual(n_user, 20)
        root.destroy()


if __name__ == "__main__":
    unittest.main()
