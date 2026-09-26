"""本轮重构的行为验收测试（原 10 项 + 3 项结构性回归）。

验收口径是「行为」而不是「长得像设计稿」：
  1. 用户发消息后正文立即可见
  2. AI 回复正常显示
  3. 点击文件树中的文件真的能看到文件内容
  4. 点击变更文件真的能看到 diff
  5. 聊天里的文件引用能打开对应文件
  6. 无文件打开时有明确 empty state（Workspace Home）
  7. Workspace 可以打开/关闭，Chat 自动适应宽度
  8. 输入区明显比之前简洁（结构性断言）
  9. 技术状态不抢过聊天正文的视觉优先级
 10. 没有删除现有 Gateway / 工具 / 任务等能力
 11. Activity Bar 与 contextual Sidebar 彻底分层
 12. 详细 telemetry 默认收起
 13. 窄窗口优先收起辅助区

运行：
    python -m unittest test_refactor_v3 -v     # 需桌面
"""
from __future__ import annotations

import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

import chat_widgets as cw  # noqa: E402
import forge_gui_v2 as gui  # noqa: E402


def find(widget, predicate, out=None):
    """深度优先找所有满足条件的子控件。"""
    if out is None:
        out = []
    for child in widget.winfo_children():
        if predicate(child):
            out.append(child)
        find(child, predicate, out)
    return out


def label_texts(widget):
    out = []
    for w in find(widget, lambda c: c.winfo_class() in ("Label", "Text")):
        try:
            if w.winfo_class() == "Label":
                out.append(str(w.cget("text")))
            else:
                out.append(w.get("1.0", "end-1c"))
        except Exception:
            continue
    return out


def w_has_text(widget, needle):
    try:
        if widget.winfo_class() == "Label":
            return needle in str(widget.cget("text"))
    except Exception:
        pass
    return False


class RefactorAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()
        cls.repo = REPO

    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("1440x900")
        self.errors = []
        self.root.report_callback_exception = lambda *a: self.errors.append(a)
        with patch.object(gui, "_autostart_enabled", return_value=False), \
             patch.object(gui.ForgeGuiApp, "_start_sysmon"), \
             patch.object(gui, "_find_run_py", return_value=self.repo / "run.py"):
            self.app = gui.ForgeGuiApp(self.root)
        self.root.update()

    def tearDown(self):
        self.app._closing = True
        for attr in ("_autostart_after_id", "_event_poll"):
            aid = getattr(self.app, attr, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def pump(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.02)
        self.root.update_idletasks()

    # ── 1. 用户消息正文立即可见（本轮最关键的 bug）──
    def test_user_message_body_is_visible_immediately(self):
        text = "把桌面端的界面重新设计一下，右边加一个可以收起的工作区"
        msg = self.app.chat_area.add_user(text)
        self.root.update_idletasks()
        found = [t for t in label_texts(msg) if text in t]
        self.assertTrue(found, "用户消息正文没有渲染出来")
        self.pump(0.3)
        body = [w for w in find(msg, lambda c: w_has_text(c, text))]
        self.assertTrue(body, "找不到正文控件")
        self.assertGreater(body[0].winfo_width(), 60, "正文控件宽度过小（会被裁掉）")

    # ── 2. AI 回复正常显示 ──
    def test_agent_reply_renders(self):
        class Client:
            base_url = "http://127.0.0.1:8799"

            def health(self):
                return True, "ok"

            def stream_chat(self, messages, **kw):
                kw["on_chunk"]("重构完成。\n\n- 消息可见\n- 工作区可开")
        self.app.client = Client()
        self.app.send_var.set("开始重构")
        self.app._do_send()
        self.pump(0.6)
        texts = " ".join(label_texts(self.app.chat_area))
        self.assertIn("重构完成", texts)

    # ── 3. 点文件树文件 → 真的有内容 ──
    def test_file_tree_click_shows_real_content(self):
        self.app._open_workspace("file_tree")
        self.pump(0.5)
        ws = self.app.workspace
        self.assertIsNotNone(ws, "工作区不可用")
        # 用一个仓库里一定存在的已追踪 .py 文件（不依赖未跟踪文件）
        target = self.repo / "forge-gui" / "forge_gui_v2.py"
        if not target.is_file():
            self.skipTest("仓库里没有目标文件")
        # 展开顶层：workspace 默认只展开第一层，要走更深
        for depth in range(6):
            rows = getattr(ws, "_file_tree_rows", [])
            row = next((r for r, node in rows
                        if not node.get("is_dir")
                        and Path(node["path"]) == target), None)
            if row is not None:
                break
            ws._expanded_dirs.add(str(target.parent))
            ws._refresh_file_tree()
            self.pump(0.2)
        if row is None:
            self.skipTest("文件树里没找到目标文件")
        row._handle_click()
        self.pump(0.5)
        code = getattr(ws, "_code_text", None)
        self.assertIsNotNone(code, "工作区没有代码控件")
        shown = code.get("1.0", "4.0")
        on_disk = "".join(target.read_text(encoding="utf-8").splitlines(keepends=True)[:3])
        self.assertTrue(shown.strip(), "点文件后代码区仍是空的")
        self.assertEqual(shown.split("\n")[0], on_disk.split("\n")[0],
                         "显示内容与磁盘文件不一致")

    # ── 4. 点变更文件 → 真的能看到 diff ──
    def test_changes_click_shows_diff(self):
        self.app._open_workspace("changes")
        self.pump(0.5)
        ws = self.app.workspace
        if not getattr(ws, "_git_status", None):
            self.skipTest("仓库当前没有未提交改动")
        rel = next(iter(ws._git_status))
        ws.show_diff(rel)
        self.pump(0.4)
        diff = getattr(ws, "_diff_text", None)
        self.assertIsNotNone(diff)
        body = diff.get("1.0", "end-1c")
        self.assertTrue(body.strip(), "diff 区是空的")

    # ── 5. 聊天里的文件引用能打开对应文件 ──
    def test_chat_file_reference_opens_file(self):
        handler = cw.__dict__.get("_FILE_LINK_HANDLER")
        self.assertIsNotNone(handler, "chat_widgets 没有注册文件引用 handler")
        rel = "forge/gateway.py"
        if not (self.repo / rel).is_file():
            self.skipTest("仓库里没有该文件")
        ok = handler(rel)
        self.pump(0.5)
        self.assertTrue(ok, "文件引用没有被处理")
        ws = self.app.workspace
        self.assertIsNotNone(ws)
        self.assertTrue(ws.is_visible, "点引用后工作区应该自动展开")
        code = getattr(ws, "_code_text", None)
        self.assertIsNotNone(code)
        self.assertTrue(code.get("1.0", "2.0").strip(), "点引用后没有打开文件内容")

    # ── 6. 无文件打开时有明确 empty state ──
    def test_workspace_home_when_no_file_open(self):
        self.app._open_workspace("file_tree")
        self.pump(0.5)
        ws = self.app.workspace
        home_texts = " ".join(label_texts(ws))
        self.assertTrue(home_texts.strip(), "无文件打开时工作区没有任何文案")
        self.assertTrue(("files changed" in home_texts)
                        or (self.repo.name in home_texts)
                        or ("变更" in home_texts),
                        f"Home 文案不可读: {home_texts[:120]}")

    # ── 7. 工作区开/关，Chat 自适应宽度 ──
    def test_workspace_toggle_widens_chat(self):
        self.app._show_view("chat")
        self.pump(0.3)
        closed = self.app.center.winfo_width()
        self.app._open_workspace("file_tree")
        self.pump(0.5)
        opened = self.app.center.winfo_width()
        self.assertLess(opened, closed, "打开工作区后聊天区应该变窄")
        self.app._close_workspace()
        self.pump(0.4)
        reopened = self.app.center.winfo_width()
        self.assertGreater(reopened, opened, "关闭工作区后聊天区应该变宽")

    # ── 8. 输入区结构简洁 ──
    def test_composer_is_single_card_with_footer_controls(self):
        card = self.app.input_card
        self.assertTrue(hasattr(card, "entry"), "缺少输入框")
        self.assertTrue(hasattr(card, "send_circle"), "缺少发送按钮")
        self.assertTrue(hasattr(card, "stop_circle"), "缺少停止按钮")
        combo = getattr(self.app, "model_combo", None)
        self.assertIsNotNone(combo, "模型选择器缺失")
        self.assertTrue(combo.winfo_ismapped(), "模型选择器不可见")
        ancestors = []
        node = combo
        while node is not None:
            ancestors.append(node)
            node = getattr(node, "master", None)
        self.assertIn(card, ancestors, "模型选择器不在输入卡内部")
        self.assertGreater(card.send_circle.winfo_rootx(), card.entry.winfo_rootx())

    # ── 9. 技术状态不抢正文 ──
    def test_tech_status_does_not_outweigh_body(self):
        import tkinter.font as tkfont
        body_font = tkfont.Font(font=gui.FONT_UI)
        status_font = tkfont.Font(font=gui.FONT_CAPTION)
        self.assertLessEqual(status_font.cget("size"), body_font.cget("size"),
                             "状态栏字号不应大于正文字号")
        msg = self.app.chat_area.add_agent(role="Planner")
        rows = [{"name": "read_file", "desc": "读文件", "elapsed": "0.4s"},
                {"name": "write_file", "desc": "写文件", "elapsed": "1.1s"}]
        card = msg.add_tool_card(rows)
        self.pump(0.3)
        # 直接按 ToolCard 内部状态断言，不去爬 widget 树（折叠时明细不在
        # mapped 路径里，靠 find_all + ismapped 会假阳性命中其它 label）
        self.assertFalse(getattr(card, "_expanded", True),
                         "ToolCard 默认应收起（_expanded 应为 False）")
        self.assertFalse(card._card.canvas.winfo_ismapped(),
                         "默认收起时 RoundedCard canvas 不应 mapped")
        card.toggle()
        self.pump(0.2)
        self.assertTrue(card._expanded, "toggle 后 _expanded 应为 True")
        self.assertTrue(card._card.canvas.winfo_ismapped(),
                        "展开后 RoundedCard canvas 应 mapped")

    # ── 10. 现有能力没被删 ──
    def test_existing_capabilities_kept(self):
        self.assertEqual(len(gui.NAV_ITEMS), 8)
        self.assertTrue(hasattr(self.app, "_more_menu"), "「更多」菜单缺失")
        for key in ("chat", "task", "tools", "config"):
            self.assertIn(key, self.app._views, f"视图 {key} 消失了")
            self.app._show_view(key)
            self.pump(0.2)
            self.assertTrue(self.app._views[key].winfo_ismapped(),
                            f"视图 {key} 打不开")
        self.assertTrue(hasattr(self.app, "gw_btn"))
        self.assertTrue(hasattr(self.app, "port_spin"))
        self.assertTrue(hasattr(self.app, "gw_status_var"))
        self.assertTrue(hasattr(self.app, "task_var"))
        self.assertTrue(callable(getattr(self.app, "_run_task", None)))

    # ── 11. Activity Bar 与 contextual Sidebar 真正分层 ──
    def test_activity_bar_and_contextual_sidebar_are_separate(self):
        self.assertEqual(len(self.app._activity_markers), len(gui.NAV_ITEMS))
        self.assertLess(self.app.activity_bar.winfo_width(),
                        self.app.sidebar.winfo_width())
        chat_panel = self.app._sidebar_panels["chat"]
        task_panel = self.app._sidebar_panels["task"]
        self.assertTrue(chat_panel.winfo_ismapped())
        self.app._show_view("task")
        self.pump(0.2)
        self.assertFalse(chat_panel.winfo_ismapped())
        self.assertTrue(task_panel.winfo_ismapped())

    # ── 12. 详细 telemetry 默认收起 ──
    def test_secondary_telemetry_is_disclosed_on_demand(self):
        self.assertFalse(self.app._telemetry_panel.winfo_ismapped())
        self.app._toggle_telemetry()
        self.pump(0.1)
        self.assertTrue(self.app._telemetry_panel.winfo_ismapped())
        self.app._toggle_telemetry()
        self.pump(0.1)
        self.assertFalse(self.app._telemetry_panel.winfo_ismapped())

    def test_workspace_open_keeps_long_messages_and_composer_inside_conversation(self):
        msg = self.app.chat_area.add_user("请检查布局并保持文件与对话均可阅读。" * 50)
        self.app._open_workspace()
        self.pump(0.3)
        right = self.app.center.winfo_rootx() + self.app.center.winfo_width()
        for widget in (msg.label, self.app.model_combo, self.app.think_pill,
                       self.app.input_card.send_circle):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth() - 2)
        self.assertGreater(msg.label.winfo_height(), 40)

    def test_workspace_auxiliary_navigation_and_file_tabs_remain_usable(self):
        self.app._open_workspace()
        self.pump(0.3)
        ws = self.app.workspace
        self.assertFalse(ws._changes_col.winfo_ismapped())
        ws.open_changes()
        self.pump(0.1)
        self.assertTrue(ws._changes_col.winfo_ismapped())
        ws._toggle_changes_nav()
        self.pump(0.1)
        self.assertFalse(ws._changes_col.winfo_ismapped())
        self.assertEqual(len(ws._vp.panes()), 3)
        ws.open_file(gui.HERE / "gui_theme.py")
        self.pump(0.3)
        self.assertTrue(ws._file_tab_strip.winfo_ismapped())
        self.assertLess(ws._file_tab_strip.winfo_rooty(), ws._code_text.winfo_rooty())

    # ── 13. 窄窗口优先收起辅助区，不挤压 Conversation ──
    def test_narrow_window_auto_collapses_auxiliary_regions(self):
        self.app._open_workspace("file_tree")
        self.pump(0.3)
        self.root.geometry("1120x720")
        self.root.update_idletasks()
        self.app._apply_responsive_layout()
        self.assertFalse(self.app._sidebar_visible)
        self.assertTrue(self.app._ws_packed)
        self.assertGreaterEqual(self.app.center.winfo_width(), 500)

        self.root.geometry("940x700")
        self.root.update_idletasks()
        self.app._apply_responsive_layout()
        self.assertFalse(self.app._ws_packed)
        self.assertTrue(self.app._workspace_auto_hidden)

        self.root.geometry("1440x900")
        self.root.update_idletasks()
        self.app._apply_responsive_layout()
        self.root.update_idletasks()
        self.assertTrue(self.app._ws_packed)
        self.assertTrue(self.app._sidebar_visible)


if __name__ == "__main__":
    unittest.main(verbosity=2)
