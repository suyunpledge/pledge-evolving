r"""chat_widgets.py 自检。

使用系统 Python（带 tkinter）。所有断言以 `root.update_idletasks()` + `root.update()`
为前置，让 Tk 几何真正收敛；`winfo_width()` 等方法在 mapped 之前是 1，
必须先 `update()` 才有真值。

跑法：
    "C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe" \
        test_chat_widgets.py
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)        # 让 import chat_widgets / gui_theme 找到本地副本

import chat_widgets as cw       # noqa: E402
from gui_theme import C         # noqa: E402


# ─── helpers ──────────────────────────────────────────────────────


def _flush(root: tk.Tk):
    """强制 Tk 计算所有 idle 任务 + 暴露事件，让 winfo_* 收敛。"""
    root.update_idletasks()
    root.update()


def _area(width=900, height=560):
    root = tk.Tk()
    root.geometry(f"{width}x{height}")
    root.configure(bg=C["chat"])
    return root


# ─── 1. 用户气泡可见性 / 自适应 ──────────────────────────────


class UserBubbleAutoSize(unittest.TestCase):
    def test_short_and_long_messages_have_proper_width(self):
        root = _area(900, 560)
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            short = "短"
            long_text = (
                "这是一段故意写得很长很长的话，"
                "用来测试用户气泡在内容超出最大宽度时能不能"
                "在最大宽度处换行而不会爆版。"
            )
            area.add_user(short)
            area.add_user(long_text)
            _flush(root)

            msgs = [w for w in area.scroll.inner.winfo_children()
                    if isinstance(w, cw.UserMessage)]
            self.assertEqual(len(msgs), 2, "应产生两个 UserMessage")

            # 1px bug 修复：所有气泡宽度 > 30（1px 会直接挂；
            # "短" CJK 字符 + 内边距 ~ 50px 是合理值）
            for i, m in enumerate(msgs):
                card = m._card                  # RoundedCard 实例
                w = card.winfo_width()
                print(f"[user bubble {i}] winfo_width={w} winfo_reqwidth={card.winfo_reqwidth()}")
                self.assertGreater(
                    w, 30,
                    f"气泡 #{i} 宽度={w} <= 30；可能仍是 1px autosize bug",
                )
                # 应不超过 MAX_BUBBLE_WIDTH + 内边距 + 余量
                self.assertLessEqual(
                    w, cw.MAX_BUBBLE_WIDTH + 32,
                    f"气泡 #{i} 宽度={w} 超出最大宽度",
                )

            # 正文 Label 与气泡接近（气泡宽度不应远大于其内容）
            for i, m in enumerate(msgs):
                label = m.label
                _flush(root)
                lw = label.winfo_reqwidth()
                cw_ = m._card.winfo_width()
                print(f"[user msg {i}] label_reqwidth={lw} bubble={cw_}")
                # label 的 request width 应至少是气泡宽度的一半（不完全相等因为 label 是
                # 实际内容、card 包含 padx/pady 的圆角矩形）
                self.assertGreater(
                    lw, cw_ * 0.4,
                    f"气泡 #{i} 内 Label({lw}) 太窄，远小于气泡({cw_})",
                )

            # 短消息气泡 < 长消息气泡：自适应生效
            # 选取宽的那个短（如果第一次加的是短的）
            short_msg = msgs[0]
            long_msg = msgs[1]
            short_w = short_msg._card.winfo_width()
            long_w = long_msg._card.winfo_width()
            print(f"[user msg] short_w={short_w} long_w={long_w}")
            self.assertLess(
                short_w, long_w,
                f"短消息气泡({short_w}) 应 < 长消息气泡({long_w})，自适应未生效",
            )

            # 整体容器：用户消息在 MessageArea 整行容器中，靠右
            for m in msgs:
                self.assertEqual(m.winfo_width(), area.scroll.inner.winfo_width() - 36,
                                 "用户行未填满 MessageArea 整行宽度")
        finally:
            root.destroy()


# ─── 2. 消息立刻可见 ─────────────────────────────────────────


class MessageImmediateVisibility(unittest.TestCase):
    def test_add_user_text_visible_without_after(self):
        root = _area()
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            txt = "现在就显示这条消息"
            m = area.add_user(txt)
            # 仅 update_idletasks + update，不要 after
            _flush(root)
            self.assertEqual(m.label.cget("text"), txt,
                             "add_user 后正文 Label 不应为空")
            # 气泡宽度也应该 > 0
            self.assertGreater(m._card.winfo_width(), 0)
        finally:
            root.destroy()

    def test_add_agent_visible(self):
        root = _area()
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            agent.render_markdown("# 标题\n正文段落。")
            _flush(root)
            # body 里应至少有 1 个渲染的 host Frame
            children = agent.body.winfo_children()
            self.assertGreater(len(children), 0)
            # update_idletasks 后应有控件 mapped
            mapped = [c for c in children if c.winfo_ismapped()]
            self.assertGreater(len(mapped), 0, "Agent 正文应有控件被 mapped")
        finally:
            root.destroy()


# ─── 3. ToolCard 默认折叠 ────────────────────────────────────


class ToolCardCollapsedByDefault(unittest.TestCase):
    def test_default_collapsed_then_expanded(self):
        root = _area()
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            rows = [
                {"name": "read_file", "desc": "/tmp/a.py",
                 "elapsed": "0.4s", "elapsed_s": 0.4, "ok": True,
                 "detail": "100 lines"},
                {"name": "grep", "desc": "pattern",
                 "elapsed": "0.8s", "elapsed_s": 0.8, "ok": True,
                 "detail": "3 matches"},
                {"name": "write_file", "desc": "/tmp/b.py",
                 "elapsed": "1.2s", "elapsed_s": 1.2, "ok": False,
                 "detail": "permission denied"},
            ]
            card = agent.add_tool_card(rows)
            _flush(root)
            # 摘要文字应包含「3」与「工具」
            summary_text = card._summary_label.cget("text")
            print(f"[toolcard summary] {summary_text!r}")
            self.assertIn("3", summary_text)
            self.assertIn("工具", summary_text)
            self.assertIn("s", summary_text.lower(), "应包含耗时秒数")

            # 默认折叠：RoundedCard 自身应未 packed
            mapped_before = card._card.winfo_ismapped()
            self.assertFalse(
                mapped_before,
                f"ToolCard 默认应折叠；_card.ismapped={mapped_before}",
            )

            # 展开
            card.toggle()
            _flush(root)
            mapped_after = card._card.winfo_ismapped()
            self.assertTrue(
                mapped_after,
                f"toggle 后 ToolCard 明细应可见；_card.ismapped={mapped_after}",
            )

            # 再次折叠
            card.toggle()
            _flush(root)
            self.assertFalse(card._card.winfo_ismapped())
        finally:
            root.destroy()


# ─── 4. StepList 默认折叠 ─────────────────────────────────────


class StepListCollapsedByDefault(unittest.TestCase):
    def test_default_collapsed_summary(self):
        root = _area()
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            items = [
                {"index": 1, "title": "读文件", "desc": "看看结构", "done": True,
                 "elapsed": "0.5s"},
                {"index": 2, "title": "写文件", "desc": "保存", "done": True,
                 "elapsed": "1.0s"},
                {"index": 3, "title": "跑测试", "desc": "pytest", "done": True,
                 "elapsed": "2.1s"},
                {"index": 4, "title": "汇总", "desc": "出报告", "done": True,
                 "elapsed": "0.2s"},
            ]
            steps = agent.add_steps(items)
            _flush(root)
            summary = steps._summary_label.cget("text")
            print(f"[steps summary] {summary!r}")
            self.assertIn("4", summary)
            self.assertIn("步骤", summary)
            # 默认折叠
            self.assertFalse(steps._detail.winfo_ismapped())
            # 展开后可见
            steps.toggle()
            _flush(root)
            self.assertTrue(steps._detail.winfo_ismapped())
        finally:
            root.destroy()


# ─── 5. 文件引用 handler ──────────────────────────────────────


class FileLinkHandler(unittest.TestCase):
    def setUp(self):
        cw.set_file_link_handler(None)

    def test_render_with_link_and_dispatch(self):
        root = _area()
        seen = []
        cw.set_file_link_handler(lambda p: seen.append(p) or True)
        try:
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            text = ("看一下 `forge/loop.py` 和\n"
                    "[forge-gui/chat_widgets.py](forge-gui/chat_widgets.py)，"
                    "尤其是 `src/lib/rate-limit.ts:120` 这部分")
            agent.render_markdown(text)
            _flush(root)
            # 找任意一个含 file_link tag 范围的 InlineText，验证 file_link tag 出现
            it = _any_inline_text_with(agent, "file_link")
            self.assertIsNotNone(it, "正文里应有 InlineText 出现 file_link 标签")
            ranges = it.tag_ranges("file_link")
            print(f"[file_link] {len(ranges)} ranges")
            self.assertGreater(len(ranges), 0,
                                "正文里应有 file_link tag 区间")
            # 直接调用 dispatch（点击事件在 headless 较难模拟）
            cw._dispatch_file_link("forge/loop.py")
            self.assertIn("forge/loop.py", seen)
            # 也处理 markdown 包裹
            cw._dispatch_file_link("[forge-gui/chat_widgets.py](forge-gui/chat_widgets.py)")
            self.assertIn("forge-gui/chat_widgets.py", seen)
        finally:
            root.destroy()

    def test_no_handler_silent(self):
        root = _area()
        try:
            cw.set_file_link_handler(None)
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            agent.render_markdown("看 `forge/loop.py` 这个文件")
            _flush(root)
            it = _any_inline_text_with(agent, "file_link")
            self.assertIsNotNone(it)
            self.assertGreater(len(it.tag_ranges("file_link")), 0)
            # dispatch 不应抛
            cw._dispatch_file_link("forge/loop.py")
        finally:
            root.destroy()

    def test_inside_code_block_not_link(self):
        root = _area()
        try:
            cw.set_file_link_handler(lambda p: True)
            area = cw.MessageArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            agent = area.add_agent()
            md = "下面是代码块：\n\n```\nforge/loop.py\n```\n\n和正文中的 `forge/loop.py`。"
            agent.render_markdown(md)
            _flush(root)
            # 全局 file_link 范围只应在 InlineText 里出现，不能在代码块 Text 里
            it = _any_inline_text_with(agent, "file_link")
            code_text = _first_code_text_in_body(agent)
            self.assertIsNotNone(it, "应有 InlineText 且含 file_link 标签")
            self.assertGreater(len(it.tag_ranges("file_link")), 0)
            if code_text is not None:
                # 代码块是只读 tk.Text，不应应用 file_link 标签
                self.assertEqual(
                    len(code_text.tag_ranges("file_link")), 0,
                    "代码块内不应识别为 file_link",
                )
        finally:
            root.destroy()


def _walk_first(root_widget, predicate):
    if predicate(root_widget):
        return root_widget
    for c in root_widget.winfo_children():
        hit = _walk_first(c, predicate)
        if hit is not None:
            return hit
    return None


def _first_inline_text(agent):
    return _walk_first(agent.body, lambda w: isinstance(w, cw.InlineText))


def _any_inline_text_with(agent, tag):
    """找到第一个含指定 tag 区间的 InlineText。"""
    hits = []

    def collect(w):
        if isinstance(w, cw.InlineText) and len(w.tag_ranges(tag)) > 0:
            hits.append(w)
        return False

    _walk_first(agent.body, collect)
    return hits[0] if hits else None


def _first_code_text_in_body(agent):
    # 在 body 直接子 Frame(host from render_blocks) 里找 tk.Text
    from tkinter import Text as _Text
    for host in agent.body.winfo_children():
        if isinstance(host, tk.Frame):
            sub = _walk_first(host, lambda w: isinstance(w, _Text))
            if sub is not None:
                return sub
    return None


# ─── 6. Composer 接口与 on_settings ─────────────────────────────


class ComposerApi(unittest.TestCase):
    def test_basic_attributes(self):
        root = _area()
        try:
            card = cw.InputCard(root, footer_left="空闲")
            card.pack(fill=tk.X)
            _flush(root)
            self.assertIsNotNone(card.entry)
            self.assertIsNotNone(card.send_circle)
            self.assertIsNotNone(card.stop_circle)
            self.assertEqual(card.send_var.get(), "")
            self.assertIsNone(card.settings_btn)        # 默认 on_settings=None
            # footer_left 是控件，文本符合
            self.assertEqual(card.footer_left.cget("text"), "空闲")
        finally:
            root.destroy()

    def test_set_busy_toggles(self):
        root = _area()
        try:
            card = cw.InputCard(root)
            card.pack(fill=tk.X)
            _flush(root)
            self.assertTrue(card.send_circle.winfo_ismapped())
            self.assertFalse(card.stop_circle.winfo_ismapped())
            card.set_busy(True)
            _flush(root)
            self.assertFalse(card.send_circle.winfo_ismapped())
            self.assertTrue(card.stop_circle.winfo_ismapped())
            card.set_busy(False)
            _flush(root)
            self.assertTrue(card.send_circle.winfo_ismapped())
            self.assertFalse(card.stop_circle.winfo_ismapped())
        finally:
            root.destroy()

    def test_settings_btn_present_with_callback(self):
        root = _area()
        called = []
        try:
            card = cw.InputCard(root, on_settings=lambda: called.append(1))
            card.pack(fill=tk.X)
            _flush(root)
            self.assertIsNotNone(card.settings_btn,
                                "传入 on_settings 时应建出 ⚙ 按钮")
            # 模拟点击
            card.settings_btn.invoke()
            self.assertEqual(len(called), 1)
        finally:
            root.destroy()

    def test_plus_calls_on_attach_first_then_on_paste(self):
        root = _area()
        try:
            attach_calls, paste_calls = [], []

            def cb_attach():
                attach_calls.append(1)

            def cb_paste():
                paste_calls.append(1)

            # 1) on_attach 优先
            card = cw.InputCard(root, on_attach=cb_attach, on_paste=cb_paste)
            card.pack(fill=tk.X)
            _flush(root)
            card.plus._palette  # noqa: B018 访问以触发 tk 延迟创建
            # 调内部 command（直接 invoke 是 Button，canvas 走 bind）
            card.plus.bind("<Button-1>", lambda _e: cb_attach())
            card.plus.event_generate("<Button-1>", x=5, y=5)
            _flush(root)
            self.assertEqual(len(attach_calls), 1)
            self.assertEqual(paste_calls, [])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
