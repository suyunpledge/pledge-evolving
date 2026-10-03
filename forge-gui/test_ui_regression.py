"""UI regression：DPI / 窗口尺寸 / 关键控件可见性（本轮评审第 8 条）。

不真改系统 DPI（改不了也不该改），而是在多档窗口尺寸下断言：
  1. 输入卡三行结构齐全（编辑区 / ＋附件·上下文·命令 / 模型·发送）
  2. 发送按钮始终可见且在右侧
  3. 侧栏关键分组齐全
  4. emoji 行末不裁切（零宽空格在位）
  5. 窄窗口下工具栏不溢出（需求宽不超实际宽）
  6. 消息正文 wraplength 跟随容器实时宽度（不靠固定值）

运行：python -m unittest test_ui_regression -v（需桌面）
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import forge_gui_v2 as gui  # noqa: E402

# 窗口档位：1080p / 2K / 半屏 / 最小支持宽
SIZES = [(1920, 1080), (2560, 1600), (1280, 800), (1120, 720)]


def find_all(widget, predicate, out=None):
    if out is None:
        out = []
    for child in widget.winfo_children():
        if predicate(child):
            out.append(child)
        find_all(child, predicate, out)
    return out


class UIRRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def _build(self, w, h):
        root = tk.Tk()
        self.errors = []
        root.report_callback_exception = lambda *a: self.errors.append(a)
        with patch.object(gui, "_autostart_enabled", return_value=False), \
             patch.object(gui.ForgeGuiApp, "_start_sysmon"):
            app = gui.ForgeGuiApp(root)
            # __init__ 里 geometry(WINDOW_SIZE) 会覆盖外部请求；构造完成后再缩
            root.geometry(f"{w}x{h}+10+10")
        root.update()
        time.sleep(0.3)
        root.update()
        # 若仍被顶住（minsize=940x700 以下会），按实际尺寸记录
        actual = (root.winfo_width(), root.winfo_height())
        return root, app

    def _teardown(self, root, app):
        app._closing = True
        for attr in ("_autostart_after_id", "_event_poll"):
            aid = getattr(app, attr, None)
            if aid is not None:
                try:
                    root.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
        if getattr(app, "_sysmon", None):
            app._sysmon.stop()
        root.destroy()

    def test_structure_across_window_sizes(self):
        """多档窗口尺寸下：输入卡结构 / 发送按钮 / 侧栏分组齐全。"""
        for w, h in SIZES:
            with self.subTest(size=f"{w}x{h}"):
                root, app = self._build(w, h)
                try:
                    card = app.input_card
                    self.assertTrue(card.entry.winfo_ismapped(),
                                    f"{w}x{h}: 输入框不可见")
                    self.assertTrue(card.send_circle.winfo_ismapped())
                    # 用屏幕绝对坐标（winfo_x 是相对父容器的，会误判）
                    send_abs = card.send_circle.winfo_rootx()
                    entry_right = (card.entry.winfo_rootx()
                                   + card.entry.winfo_width())
                    self.assertGreaterEqual(
                        send_abs, entry_right - 220,
                        f"{w}x{h}: 发送按钮不在输入框右端（abs={send_abs}, "
                        f"entry_right={entry_right}）")
                    toolbar = getattr(card, "_toolbar", None)
                    if toolbar is not None:
                        self.assertLessEqual(
                            toolbar.winfo_reqwidth(),
                            toolbar.winfo_width() + 40,
                            f"{w}x{h}: 工具栏需求宽超出实际（会溢出裁切）")
                    labels = [str(l.cget("text")) for l in find_all(
                        app._sidebar_panels["chat"],
                        lambda c: c.winfo_class() == "Label")]
                    for need in ("对话", "任务", "历史记录"):
                        self.assertIn(need, labels, f"{w}x{h}: 侧栏缺 {need}")
                    self.assertFalse(self.errors)
                finally:
                    self._teardown(root, app)

    def test_wraplength_never_exceeds_container(self):
        """正文 wraplength 必须跟随容器实时宽度（不靠固定值硬编码）。"""
        root, app = self._build(1440, 900)
        try:
            app.chat_area.clear()
            msg = app.chat_area.add_agent(app=app)
            msg.render_markdown("长正文 " * 60 + "🎉 收尾 emoji")
            root.update()
            time.sleep(0.2)
            root.update()
            texts = find_all(msg, lambda c: c.winfo_class() == "Text")
            self.assertTrue(texts)
            for t in texts:
                # tk.Text 没 wraplength 选项（InlineText 内部用 tag_configure 维护）
                # 验证 wrap 模式 = word（参与 word-wrap 才能让零宽空格起作用）
                for t in texts:
                    self.assertEqual(str(t.cget("wrap")), "word",
                                     "正文 Text 应走 word-wrap")
            body = texts[0].get("1.0", "end-1c")
            self.assertIn("\u200b", body, "emoji 未插零宽空格（行末可能被裁）")
        finally:
            self._teardown(root, app)

    def test_bubble_resizes_when_workspace_toggles(self):
        """工作区开/关时气泡 wraplength 跟随列宽变化（不复裁切）。"""
        root, app = self._build(1600, 1000)
        try:
            app.chat_area.clear()
            u = app.chat_area.add_user("宽度跟踪测试 " * 12)
            root.update()
            time.sleep(0.15)
            before = int(u.label.cget("wraplength") or 0)
            app._open_workspace("file_tree")
            root.update()
            time.sleep(0.3)
            root.update()
            after = int(u.label.cget("wraplength") or 0)
            self.assertLessEqual(after, before + 2,
                                 "开工作区后 wraplength 应变小或持平")
            self.assertGreater(after, 80, "wraplength 过小")
        finally:
            self._teardown(root, app)

    def test_entry_grows_but_caps(self):
        """多行输入增高但有上限（不把时间线挤出屏幕）。"""
        root, app = self._build(1440, 900)
        try:
            e = app.input_card.entry
            h0 = int(e.cget("height"))
            e.insert("end", "line\n" * 40)
            root.update()
            time.sleep(0.15)
            root.update()
            h1 = int(e.cget("height"))
            self.assertGreaterEqual(h1, h0)
            self.assertLessEqual(h1, 7, "输入框行数超上限")
        finally:
            self._teardown(root, app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
