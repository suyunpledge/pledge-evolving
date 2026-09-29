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
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
# forge 包在仓库根（GUI 本身用不到，但采样策略的断言要直接引用）
sys.path.insert(0, str(REPO))

import chat_widgets as cw  # noqa: E402
import brand_marks  # noqa: E402
import decor  # noqa: E402
import gui_theme as theme  # noqa: E402
import reasoning_slider as rs  # noqa: E402
import forge_gui_v2 as gui  # noqa: E402


class BrandMarkTests(unittest.TestCase):
    """品牌标志识别：纯逻辑，不需要桌面。"""

    CASES = (
        ("mimo", "mimo"), ("mimopro-ultra", "mimo"),
        ("deepseekflash", "deepseek"), ("deepseek-v4-pro", "deepseek"),
        ("千问max", "qwen"), ("qwen3.8-flash", "qwen"),
        ("Kimi-k2.6", "kimi"), ("moonshot-v1", "kimi"),
        ("豆包", "doubao"), ("doubao-seed-evolving", "doubao"),
        ("MiniMax", "minimax"), ("claude", "claude"), ("claude-opus-5-5", "claude"),
        ("gpt", "chatgpt"), ("gpt-6-sol", "chatgpt"),
        ("GLM5.3", "glm"), ("glm-5.3", "glm"),
        ("Step-3.5-Flash-2603", "stepfun"),
        ("百灵", "ling"), ("ling-3.0-flash", "ling"),
        ("muse-spark", "muse"), ("grok-4", "grok"), ("gemini-3-pro", "gemini"),
        ("spark-4.0", "spark"),
    )

    def test_detects_brand_from_model_name(self):
        for model, expected in self.CASES:
            with self.subTest(model=model):
                brand = brand_marks.detect(model=model)
                self.assertIsNotNone(brand, f"{model} 未识别出品牌")
                self.assertEqual(brand.key, expected)

    def test_detects_brand_from_upstream_domain(self):
        cases = (
            ("some-unknown-model", "https://api.moonshot.cn/v1", "kimi"),
            ("some-unknown-model", "https://open.bigmodel.cn/api/coding/paas/v4", "glm"),
            ("some-unknown-model", "https://api.minimaxi.com/v1", "minimax"),
            ("some-unknown-model", "https://ark.cn-beijing.volces.com/api/plan/v3", "doubao"),
            ("some-unknown-model", "https://api.xiaomimimo.com/v1", "mimo"),
        )
        for model, url, expected in cases:
            with self.subTest(url=url):
                brand = brand_marks.detect(model=model, base_url=url)
                self.assertIsNotNone(brand, f"{url} 未识别")
                self.assertEqual(brand.key, expected)

    def test_provider_row_uses_its_own_model_field(self):
        row = {"baseURL": "https://api.stepfun.com/step_plan/v1", "model": "step-5-preview"}
        brand = brand_marks.detect(provider=row)
        self.assertIsNotNone(brand)
        self.assertEqual(brand.key, "stepfun")

    def test_unknown_model_yields_no_brand(self):
        self.assertIsNone(brand_marks.detect(model="zzz-unmapped-9k"))
        self.assertEqual(brand_marks.label_for(model="zzz-unmapped-9k"), "")

    def test_generated_assets_exist_for_every_brand(self):
        missing = []
        for brand in brand_marks.BRANDS:
            for size in (16, 20, 24, 32):
                if brand_marks.mark_path(brand, size) is None:
                    missing.append(f"{brand.key}-{size}")
        self.assertEqual(missing, [], f"缺少位图：{missing}")


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

    def test_model_picker_keeps_combobox_compat_and_shows_brand_mark(self):
        """模型选择器：兼容旧接口（values 读写），且当前模型带厂商标志。"""
        picker = self.app.model_combo
        picker.configure(values=["default", "mimo", "千问max"])
        self.assertEqual(list(picker["values"]), ["default", "mimo", "千问max"])
        self.assertEqual(list(picker.cget("values")), ["default", "mimo", "千问max"])
        self.assertEqual(picker.get(), self.app.model_var.get())
        self.assertEqual(picker.brand_of("千问max").key, "qwen")
        self.app.model_var.set("千问max")
        self.pump(0.1)
        self.assertTrue(picker._icon.winfo_ismapped(), "已知模型应显示厂商标志")
        self.assertEqual(picker._text.cget("text"), "千问max")
        self.app.model_var.set("zzz-unmapped-9k")
        self.pump(0.1)
        self.assertFalse(picker._icon.winfo_ismapped())

    def test_model_picker_product_surface_uses_real_router_and_filters(self):
        picker = self.app.model_combo
        providers = {
            "cloud-fast": {"model": "cloud-fast", "baseURL": "https://api.example.com/v1"},
            "local-code": {"model": "local-code", "baseURL": "http://127.0.0.1:11434/v1"},
            "default": {"model": "cloud-fast", "baseURL": "https://api.example.com/v1"},
        }
        picker._provider_lookup = lambda model: providers.get(model)
        picker._router_lookup = lambda: {
            "primary": ["cloud", "cloud-fast"],
            "routing": {"strategy": "medium", "tiers": [["cloud", "cloud-fast"]]},
        }
        picker.configure(values=["default", "cloud-fast", "local-code"])
        picker.open_menu()
        self.pump(0.2)
        self.assertEqual(picker._popup.winfo_width(), min(448, picker.winfo_screenwidth() - 16))
        pop = picker._popup
        self.assertLessEqual(abs(pop.winfo_rootx() + pop.winfo_width() -
                                 picker.winfo_rootx() - picker.winfo_width()), 2)
        self.assertLessEqual(abs(picker.winfo_rooty() -
                                 pop.winfo_rooty() - pop.winfo_height() - 6), 2)
        self.assertEqual(picker._recommended_models(), {"cloud-fast"})
        picker._set_filter("local")
        self.pump(0.1)
        self.assertEqual([value for value, _row in picker._rows], ["local-code"])
        picker._favorites.add("cloud-fast")
        picker._set_filter("favorites")
        self.pump(0.1)
        self.assertEqual([value for value, _row in picker._rows], ["cloud-fast"])
        picker.close_menu()

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
        for key in ("agents", "knowledge", "evolution", "files"):
            self.assertFalse(self.app._nav_widgets[key][0][0].winfo_ismapped(),
                             f"占位视图 {key} 不应占用主轨道")
        self.assertLess(self.app.activity_bar.winfo_width(),
                        self.app.sidebar.winfo_width())
        chat_panel = self.app._sidebar_panels["chat"]
        task_panel = self.app._sidebar_panels["task"]
        self.assertTrue(chat_panel.winfo_ismapped())
        self.app._show_view("task")
        self.pump(0.2)
        self.assertFalse(chat_panel.winfo_ismapped())
        self.assertFalse(self.app._sidebar_visible,
                         "无辅助内容的任务视图应把宽度留给时间线")
        self.app._toggle_sidebar()
        self.pump(0.1)
        self.assertTrue(task_panel.winfo_ismapped(), "用户仍可按需展开侧栏")

    def test_start_actions_lead_to_real_workflows(self):
        self.assertEqual(self.app._active_view, "chat")
        task_action = find(self.app.chat_area, lambda w: w.winfo_class() == "Label"
                           and str(w.cget("text")) == "交给 Forge 一个任务")
        self.assertEqual(len(task_action), 1)
        task_action[0].event_generate("<Button-1>")
        self.pump(0.1)
        self.assertEqual(self.app._active_view, "task")
        self.assertLess(self.app.task_area.winfo_rooty(),
                        self.app.task_run_btn.winfo_rooty(),
                        "任务输入应位于时间线底部")

        self.app._show_view("chat")
        workspace_action = find(self.app.chat_area, lambda w: w.winfo_class() == "Label"
                                and str(w.cget("text")) == "查看项目工作区")
        self.assertEqual(len(workspace_action), 1)
        workspace_action[0].event_generate("<Button-1>")
        self.pump(0.2)
        self.assertTrue(self.app._ws_packed)

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
        for widget in (msg.label, self.app.model_combo, self.app.session_menu_btn,
                       self.app.input_card.send_circle):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth() - 2)
        self.assertGreater(msg.label.winfo_height(), 40)

    def test_history_search_reaches_older_sessions_and_focuses_input(self):
        sessions = [{"id": f"past-{i}", "title": f"历史条目 {i}",
                     "updated": time.time() - i * 86400,
                     "messages": [{"role": "user", "content": f"历史问题 {i}"}]}
                    for i in range(25)]
        with patch.object(self.app, "_load_sessions", return_value=sessions), \
             patch.object(self.app, "_archive_current_session"):
            self.app._refresh_history()
            self.assertIn("历史条目 24", label_texts(self.app.history_box))
            self.app._conversation_shortcut("search")
            self.app.session_search_var.set("条目 24")
            self.pump(.1)
            self.assertIn("历史条目 24", label_texts(self.app.history_box))
            self.assertNotIn("历史条目 0", label_texts(self.app.history_box))
            self.root.focus_force()
            self.app._load_session("past-24")
            self.pump(.1)
            self.assertEqual(self.app.chat_title_var.get(), "历史条目 24")
            self.assertEqual(self.root.focus_get(), self.app.send_entry)

    def test_model_picker_combines_model_and_five_thinking_levels(self):
        self.assertFalse(self.app.think_pill.winfo_ismapped())
        self.app.model_combo.open_menu()
        self.pump(.1)
        slider = getattr(self.app.model_combo, "_thinking_slider", None)
        self.assertIsNotNone(slider, "打开弹层后应显示思考强度滑杆")
        self.assertIsInstance(slider, rs.ReasoningSlider)
        # 是「滑杆」而不是一排点选按钮：可拖动取值，且弹层保持打开
        self.assertIs(self.app.model_combo._thinking_widgets, slider.label_widgets)
        self.assertEqual(list(slider.stops),
                         ["off", "low", "medium", "high", "contemplate"])
        labels = [label.cget("text") for _host, label in slider.label_widgets.values()]
        with patch.object(gui, "save_desktop_config"), \
             patch.object(self.app, "_set_thinking_mode", return_value=True):
            slider.set_index(len(slider.stops) - 1)
            self.pump(.05)
            self.assertIsNotNone(self.app.model_combo._popup,
                                 "slider must not close the popup")
            self.assertEqual(slider.current_value(), "contemplate")
            self.assertEqual(self.app.reasoning_var.get(), "contemplate")
            slider.step(-1)
            self.pump(.05)
            self.assertEqual(slider.current_value(), "high")
            slider.set_index(0)
            self.pump(.05)
            self.assertEqual(slider.current_index(), 0)
            slider.reset()
            self.pump(.05)
            self.assertEqual(slider.current_value(), "medium")
            self.assertTrue(slider.canvas.find_withtag("slider-card"))
            self.assertTrue(slider.canvas.find_withtag("slider-track-active"))
            self.assertTrue(slider.canvas.find_withtag("slider-thumb"))
            self.assertEqual(slider.canvas.itemcget(
                slider.canvas.find_withtag("slider-model")[0], "text"),
                self.app.model_combo._current_model_label())
        self.assertEqual(labels, ["Light", "Standard", "Deep", "Intense", "Scrutiny"])
        self.app.model_combo.close_menu()
        with patch.object(self.app, "_set_thinking_mode", return_value=True) as set_mode, \
             patch.object(gui, "save_desktop_config") as save:
            self.app._thinking_mode = "off"
            self.assertTrue(self.app._set_reasoning_effort("high"))
            set_mode.assert_called_once_with("smart", announce=False)
            save.assert_called_once_with(reasoning_effort="high")
            self.assertEqual(self.app.reasoning_var.get(), "high")
            self.assertIn("思考强度 · Intense", self.app.model_combo._thinking_text.cget("text"))

    # ── 15. 侧边栏可见名称 + 彩色 emoji ──────────────────────
    def test_activity_bar_shows_names_and_colour_emoji(self):
        labels = {}
        for key, entries in self.app._activity_labels.items():
            for _icon, text in entries:
                labels[key] = text.cget("text")
        for key in gui.PRIMARY_NAV:
            self.assertEqual(labels.get(key), gui.NAV_LABEL[key],
                             f"{key} 的侧边栏入口应显示名称")
        # 图标必须是真 emoji（配 emoji 字体才出彩色），不是单色 dingbat
        for _key, label, glyph in gui.NAV_ITEMS:
            with self.subTest(glyph=glyph):
                self.assertTrue(theme.is_emoji(glyph), f"{glyph} 不是 emoji 字形")
                self.assertEqual(theme.emoji_font(glyph)[0], theme.EMOJI_FAMILY)
        self.assertTrue(theme.is_emoji("💬") and not theme.is_emoji("▣"))
        # 轨道仍要窄于 Sidebar（一级/二级分层不变）
        self.assertLess(self.app.activity_bar.winfo_width(),
                        self.app.sidebar.winfo_width())

    # ── 16. 对话两侧都有气泡 ────────────────────────────────
    def test_both_sides_of_conversation_have_bubbles(self):
        user = self.app.chat_area.add_user("用户消息")
        agent = self.app.chat_area.add_agent()
        agent.render_markdown("回复正文。")
        self.pump(0.3)
        self.assertEqual(user._card._fill, theme.C["msg_user_bg"])
        self.assertEqual(user._card._outline, theme.C["msg_user_border"])
        bubble = getattr(agent, "_bubble", None)
        self.assertIsNotNone(bubble, "agent 回复也应有气泡")
        self.assertEqual(bubble._fill, theme.C["msg_agent_bg"])
        self.assertEqual(bubble._outline, theme.C["msg_agent_border"])
        # 两种气泡底色必须不同，否则「谁在说」看不出来
        self.assertNotEqual(theme.C["msg_user_bg"], theme.C["msg_agent_bg"])
        self.assertTrue(agent.body.winfo_ismapped(), "气泡内正文必须可见")
        self.assertGreater(agent.body.winfo_height(), 1)
        # 气泡不能横向溢出对话列
        self.assertLessEqual(bubble.winfo_width(),
                             max(1, agent._bubble_host.winfo_width()))

    # ── 17. 智能路由可被用户控制并落盘 ──────────────────────
    def test_router_strategy_is_user_controllable(self):
        self.app.model_combo.open_menu()
        self.pump(0.2)
        chips = self.app.model_combo._router_chips
        self.assertEqual(set(chips), {"base", "medium", "premium"})
        before = self.app.model_combo._current_router_strategy()
        target = "premium" if before != "premium" else "base"
        with patch.object(gui, "save_user_layer") as save:
            self.assertTrue(self.app._set_router_strategy(target))
            save.assert_called_once()
        self.assertEqual(self.app._task_strategy, target)
        self.assertEqual(self.app.model_combo._current_router_strategy(), target)
        # 选完不关弹层（还能接着调模型 / 思考强度）
        self.assertIsNotNone(self.app.model_combo._popup)
        self.app.model_combo.close_menu()
        # 非法策略拒绝
        self.assertFalse(self.app._set_router_strategy("nonsense"))

    # ── 18. API 密钥面板 ────────────────────────────────────
    def test_api_key_panel_lists_and_masks_providers(self):
        targets = self.app._key_targets()
        self.assertTrue(targets, "应能列出需要密钥的 provider")
        self.assertEqual(self.app._masked_key("sk-abcdef123456"), "sk-abc…3456")
        self.assertEqual(self.app._masked_key(""), "")
        before = len(self.root.winfo_children())
        self.app._open_api_keys()
        self.pump(0.3)
        dialogs = [w for w in self.root.winfo_children()
                   if w.winfo_class() == "Toplevel"]
        self.assertTrue(dialogs, "API 密钥面板应能打开")
        self.assertIn("已配置", self.app.key_status_var.get())
        for d in dialogs:
            try:
                d.grab_release()
                d.destroy()
            except Exception:
                pass
        self.assertGreaterEqual(len(self.root.winfo_children()), before)

    # ── 19. 自启重试的失败路径（评审 P1/P2）──────────────────
    def test_gateway_timeout_kills_the_real_process_and_counts_the_attempt(self):
        """超时清理要传进程对象（不是 pid），且超时也算一次尝试。"""
        proc = Mock()
        proc.poll.return_value = None
        self.app.gateway_proc = proc
        self.app._gateway_autostarted = True
        self.app._gateway_user_stopped = False
        self.app._autostart_attempts = 0
        with patch.object(gui, "kill_process_tree") as kill, \
             patch.object(self.app, "_schedule_autostart_retry") as retry:
            self.app._gateway_timeout(proc)
        # 关键：传的是进程对象；传 pid 会让 kill_process_tree 内部 proc.poll() 抛错
        kill.assert_called_once()
        self.assertIs(kill.call_args.args[0], proc)
        # 超时也要计入尝试次数，否则退避索引为 -1（取到最后一档）且永不封顶
        self.assertEqual(self.app._autostart_attempts, 1)
        retry.assert_called_once()

    def test_user_stop_cancels_a_queued_autostart_retry(self):
        """用户在本轮重试排队后按了停止：定时器到点也不能再拉起 gateway。"""
        self.app._gateway_user_stopped = True
        self.app._autostart_attempts = 1
        with patch.object(self.app, "_start_gateway") as start:
            self.app._autostart_gateway()
        start.assert_not_called()

    def test_retry_backoff_index_is_never_negative(self):
        """_autostart_attempts=0 时不能索引到最后一档。"""
        self.app._gateway_user_stopped = False
        self.app._autostart_attempts = 0
        seen = []
        with patch.object(gui, "_autostart_enabled", return_value=True), \
             patch.object(self.app, "root") as root:
            root.after.side_effect = lambda ms, fn: seen.append(ms / 1000.0)
            self.app._schedule_autostart_retry("probe")
        self.assertEqual(seen, [gui.AUTOSTART_RETRY_DELAYS[0]])

    # ── 20. 密钥面板与配置引用必须一致（评审 P1）──────────────
    def test_api_key_panel_repairs_a_mismatched_env_ref(self):
        """整理器生成的随机引用（FORGE_KEY_XXXX）应在保存后被改写为面板注入的名字。"""
        rid = "custom__probe"
        canonical = self.app._canonical_key_env(rid)
        self.assertTrue(canonical.startswith("FORGE_") and canonical.endswith("_KEY"))
        row = {"id": rid, "name": f"provider:{rid}", "config": {
            "wire": "openai", "baseURL": "https://api.example.com/v1",
            "model": "probe-1",
            "apiKey": {"$expr": "get('env.FORGE_KEY_DEADBE', '')"}}}
        self.app.user_rows = [row] + [
            r for r in self.app.user_rows if str(r.get("id")) != rid]
        self.assertEqual(self.app._key_env_name(row["config"]), "FORGE_KEY_DEADBE")
        with patch.object(gui, "load_user_layer", return_value=[row.copy()]), \
             patch.object(gui, "save_user_layer") as save:
            repaired = self.app._repair_key_ref(rid)
        self.assertTrue(repaired, "引用不一致时必须改写")
        saved = save.call_args.args[1]
        target = next(r for r in saved if r["id"] == rid)
        self.assertEqual(self.app._key_env_name(target["config"]), canonical)
        # 已经一致时不再重复改写
        with patch.object(gui, "load_user_layer",
                          return_value=[target]), \
             patch.object(gui, "save_user_layer") as save2:
            self.assertFalse(self.app._repair_key_ref(rid))
        save2.assert_not_called()

    # ── 21. 路由保存失败不得显示为已选中（评审 P2）────────────
    def test_router_chip_reverts_when_save_fails(self):
        self.app.model_combo.open_menu()
        self.pump(0.2)
        picker = self.app.model_combo
        before = picker._current_router_strategy()
        other = "base" if before != "base" else "premium"
        # 必须拦 picker 持有的那个回调（它是在构造时绑定的）；
        # patch 实例方法拦不住，会真的走保存——那会写到真实配置。
        # 再叠一层 save_user_layer 兼底，确保测试不会改动磁盘。
        with patch.object(picker, "_on_router_strategy", return_value=False) as cb, \
             patch.object(gui, "save_user_layer") as save:
            picker._pick_router(other)
        self.pump(0.1)
        cb.assert_called_once_with(other)
        save.assert_not_called()
        # 失败后胶囊高亮必须回到真实策略，而不是所点的那一档
        self.assertEqual(picker._current_router_strategy(), before)
        self.assertIn(before, picker._router_heading.cget("text"))
        picker.close_menu()

    # ── 22. Base 说明与真实行为一致（评审 P2）─────────────────
    def test_base_strategy_hint_matches_climb_behaviour(self):
        hints = {v: h for v, _l, h in gui.STRATEGY_CHOICES}
        self.assertIn("升级", hints["base"])
        self.assertNotIn("只用", hints["base"])

    # ── 23. 一键配置：采样温度（全部 mock，不碰真实配置文件）─────
    def test_one_click_temperature_preset(self):
        import forge.sampling as sampling
        rows = [
            {"id": "medium", "name": "provider:medium", "config": {
                "wire": "openai", "baseURL": "https://api.deepseek.com",
                "model": "deepseek-v4",
                "apiKey": {"$expr": "get('env.K', '')"}}},
            {"id": "claude_p", "name": "provider:claude", "config": {
                "wire": "anthropic", "baseURL": "https://api.anthropic.com",
                "model": "claude-sonnet-4-5",
                "apiKey": {"$expr": "get('env.C', '')"}}},
            {"id": "model", "name": "model:router", "config": {
                "primary": ["medium", "deepseek-v4"], "fallback": []}},
        ]
        self.app.user_rows = rows

        def _capture(value, current=None):
            """用假的保存/读取跑一遍 handler，返回实际生效的行。

            「不设置」在本来就没有 temperature 时无事可写（不会调用保存），
            这时返回当前行即可——产品行为就是这样，测试要跟着它走。
            """
            current = rows if current is None else current
            snapshot = [dict(r, config=dict(r["config"])) for r in current]
            with patch.object(gui, "load_user_layer",
                              return_value=[dict(r, config=dict(r["config"]))
                                            for r in snapshot]), \
                 patch.object(gui, "save_user_layer") as save:
                self.assertTrue(self.app._apply_temperature_preset(value))
            if save.called:
                return save.call_args.args[1]
            return snapshot

        written = _capture("0.7")
        temps = {r["id"]: (r.get("config") or {}).get("temperature")
                 for r in written if "baseURL" in (r.get("config") or {})}
        self.assertEqual(temps, {"medium": 0.7, "claude_p": 0.7})
        # 其它键必须原样保留（温度只是多一个字段）
        medium = next(r for r in written if r["id"] == "medium")
        self.assertEqual(medium["config"]["model"], "deepseek-v4")
        self.assertIn("apiKey", medium["config"])

        written = _capture("1.0")
        temps = {r["id"]: (r.get("config") or {}).get("temperature")
                 for r in written if "baseURL" in (r.get("config") or {})}
        self.assertEqual(temps, {"medium": 1.0, "claude_p": 1.0})

        # 「不设置」= 把字段摘掉，回到服务端默认（从「已设 1.0」的状态出发）
        seeded = [dict(r, config=dict(r["config"], temperature=1.0))
                  for r in written]
        cleared = _capture("", current=seeded)
        temps = {r["id"]: (r.get("config") or {}).get("temperature")
                 for r in cleared if "baseURL" in (r.get("config") or {})}
        self.assertEqual(temps, {"medium": None, "claude_p": None})
        # 本来就没有 temperature 时，「不设置」应当是空操作（不写盘）
        again = _capture("", current=cleared)
        temps = {r["id"]: (r.get("config") or {}).get("temperature")
                 for r in again if "baseURL" in (r.get("config") or {})}
        self.assertEqual(temps, {"medium": None, "claude_p": None})

        # 发送层最终裁决：Claude 与只认默认温度的模型都拿不到 temperature
        self.assertIsNone(sampling.resolve("claude-sonnet-4-5", "anthropic", 0.7))
        self.assertIsNone(sampling.resolve("gpt-6", "openai", 0.7))
        self.assertEqual(sampling.resolve("deepseek-v4", "openai", 0.7), 0.7)
        # 文案与行为一致
        self.assertIn("1.0", sampling.describe("gpt-6", "openai", 0.7))
        self.assertIn("服务端默认", sampling.describe("claude-sonnet-4-5",
                                                    "anthropic", 0.7))

    # ── 24. 供应商目录（含套餐端点）────────────────────────
    def test_provider_catalog_lists_every_provider_and_plan(self):
        import provider_catalog as catalog
        # 用户点名的供应商都要在
        names = " ".join(p.name for p in catalog.PRESETS)
        for wanted in ("七牛", "硅基流动", "深度求索", "Anthropic", "Google",
                       "OpenAI", "xAI", "智谱", "千问", "月之暗面",
                       "MiniMax"):
            with self.subTest(wanted=wanted):
                self.assertIn(wanted, names)
        # MiniMax 国内 / Global 必须分成两条
        self.assertIn("minimax", catalog.BY_KEY)
        self.assertIn("minimax_global", catalog.BY_KEY)
        # 每家至少一个接入方式，且都有 https 地址
        for preset in catalog.PRESETS:
            with self.subTest(provider=preset.key):
                self.assertTrue(preset.plans, f"{preset.key} 没有任何接入方式")
                for plan in preset.plans:
                    self.assertTrue(plan.base_url.startswith("https://"),
                                    f"{preset.key}/{plan.label} 地址不合法")
                    self.assertIn(plan.wire, ("openai", "anthropic"))
        # 订阅套餐要单独列，而不是和标准 API 混成一档
        zhipu = catalog.BY_KEY["zhipu"]
        labels = [pl.label for pl in zhipu.plans]
        self.assertIn("Coding Plan", labels)
        self.assertIn("标准 API", labels)
        self.assertNotEqual(
            catalog.BY_KEY["zhipu"].plans[0].base_url,
            catalog.BY_KEY["zhipu"].plans[1].base_url,
            "标准 API 与 Coding Plan 的端点必须不同")
        # Kimi Code 订阅双协议
        kimi = catalog.BY_KEY["moonshot"]
        wires = {pl.wire for pl in kimi.plans}
        self.assertEqual(wires, {"openai", "anthropic"})
        # 来源要如实分层：用户确认 > 本机配置 > 官方文档 > 待确认
        self.assertTrue(any(p.source == catalog.SOURCE_CONFIRMED
                            for p in catalog.PRESETS), "应有用户确认的条目")
        self.assertTrue(any(pl.effective_source(p) == catalog.SOURCE_UNVERIFIED
                            for p, pl in catalog.all_plans()),
                        "未核实的端点必须标「待确认」，不能装作已核对")
        # 用不了的方式要置灰（usable=False），不能生成装不上的模板
        unusable = [(p.key, pl.label) for p, pl in catalog.all_plans()
                    if not getattr(pl, "usable", True)]
        self.assertTrue(unusable, "应有明确标记为不可用的接入方式")
        for key, _label in unusable:
            with self.subTest(provider=key):
                self.assertEqual(key, "google")
        with self.assertRaises(ValueError):
            preset, plan = next((p, pl) for p, pl in catalog.all_plans()
                                if not getattr(pl, "usable", True))
            catalog.config_snippet(preset, plan)
        # 用户直接给定的地址必须原样落库
        expected = {
            ("minimax_global", "https://api.minimax.io/v1"),
            ("siliconflow", "https://api.siliconflow.com/v1"),
            ("siliconflow", "https://api.siliconflow.cn/v1"),
            ("xai", "https://api.x.ai/v1"),
            ("spark", "https://spark-api-open.xf-yun.com/v1"),
            ("hunyuan", "https://api.hunyuan.cloud.tencent.com/v1"),
            ("sensenova", "https://token.sensenova.cn/v1"),
            ("sensenova", "https://api.sensenova.cn/compatible-mode/v2"),
            ("google", "https://generativelanguage.googleapis.com"),
        }
        have = {(p.key, pl.base_url) for p, pl in catalog.all_plans()}
        for item in expected:
            with self.subTest(item=item):
                self.assertIn(item, have)
        # 搜索能按别名命中
        self.assertIn("moonshot", [p.key for p in catalog.find("kimi")])
        self.assertIn("qiniu", [p.key for p in catalog.find("七牛")])
        # 目录里每一家都必须能拿到品牌图标，且位图四档齐全
        # （这次就是靠这条发现 siliconflow / qiniu 没图标、另有 4 家键名对不上）
        for preset in catalog.PRESETS:
            with self.subTest(provider=preset.key):
                self.assertTrue(preset.brand,
                                f"{preset.key} 没指定品牌")
                for size in (16, 20, 24, 32):
                    self.assertIsNotNone(
                        brand_marks.mark_path(preset.brand, size),
                        f"{preset.key} 缺 {size}px 图标（brand={preset.brand}）")

        # 生成的模板含 baseURL/wire，且**绝不含密钥**
        snippet = catalog.config_snippet(*catalog.all_plans()[0])
        self.assertIn("baseURL:", snippet)
        self.assertIn("wire:", snippet)
        self.assertNotIn("sk-", snippet)

    def test_claude_five_series_never_sends_sampling_params(self):
        """Claude 只有 5 系（没有 6 系）：一律不发 temperature / top_p。"""
        import forge.sampling as sampling
        for model in ("claude-opus-5-5", "claude-sonnet-5", "claude-5-luna",
                      "claude-sonnet-4-5"):
            with self.subTest(model=model):
                self.assertIsNone(sampling.resolve(model, "openai", 0.7))
                self.assertIsNone(sampling.resolve(model, "anthropic", 0.7))
                self.assertIn("服务端默认",
                              sampling.describe(model, "anthropic", 0.7))
        # GPT 才有 6 系（gpt-6 / gpt-6-sol），只认默认温度
        for model in ("gpt-6", "gpt-6-sol", "sol", "kimi-k3", "kimi-k2.6"):
            with self.subTest(model=model):
                self.assertIsNone(sampling.resolve(model, "openai", 0.7))
        # 普通模型仍可用 0.7
        self.assertEqual(sampling.resolve("deepseek-v4", "openai", 0.7), 0.7)



    def test_empty_state_shows_decorated_banner(self):
        """欢迎屏应挂上主视觉画布（decor 标签、可见、不越界）。"""
        import json as _json
        app = self.app
        app.chat_area.show_empty()
        self.pump(0.2)

        def walk(w, acc):
            try:
                if w.winfo_class() == "Canvas":
                    tagged = w.find_withtag("decor")
                    if tagged:
                        acc.append(w)
            except Exception:
                pass
            for c in w.winfo_children():
                walk(c, acc)

        found = []
        walk(app.chat_area, found)
        self.assertTrue(found, "欢迎屏应有带 decor 标签的画布")
        hero = found[0]
        self.assertTrue(hero.winfo_ismapped(), "主视觉必须可见")
        self.assertGreater(len(hero.find_withtag("decor")), 100)
        for i in hero.find_all():
            b = hero.bbox(i)
            if b:
                self.assertLessEqual(b[2], hero.winfo_width() + 1)
                self.assertLessEqual(b[3], hero.winfo_height() + 1)


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


class DecorTests(unittest.TestCase):
    """装饰图案库：Tk 原生、无动画、颜色向背景渐混。"""

    def setUp(self):
        self.host = tk.Tk()
        self.host.withdraw()

    def tearDown(self):
        self.host.destroy()

    def test_primitives_draw_soft_and_deterministic(self):
        bg = "#0E0E12"
        c = tk.Canvas(self.host, width=300, height=100, bg=bg,
                      highlightthickness=0)
        n = decor.dot_grid(c, 5, 5, 295, 95, bg=bg)
        self.assertGreaterEqual(n, 60)
        fills = {c.itemcget(i, "fill") for i in c.find_all()}
        self.assertEqual(len(fills), 2, "点阵应有两种深度")
        self.assertNotIn(bg, fills, "点阵颜色不能等于背景（否则不可见）")

        c2 = tk.Canvas(self.host, width=200, height=160, bg=bg,
                       highlightthickness=0)
        decor.soft_orb(c2, 100, 80, 60, bg=bg, layers=9, core="#EDE9FE")
        rings = sorted(c2.find_all(), key=lambda i: c2.bbox(i)[2] - c2.bbox(i)[0])
        fills2 = [c2.itemcget(i, "fill") for i in rings
                  if c2.itemcget(i, "fill") != "#EDE9FE"]

        def bgness(hexcolor):
            fg = (0x7C, 0x3A, 0xED)
            bgc = (0x0E, 0x0E, 0x12)
            h = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
            df = sum(abs(h[k] - fg[k]) for k in range(3))
            db = sum(abs(h[k] - bgc[k]) for k in range(3))
            return db / max(1, df + db)

        ratios = [bgness(f) for f in fills2]
        self.assertTrue(all(ratios[k] > ratios[k + 1]
                            for k in range(len(ratios) - 1)),
                        "光晕必须由外到内单调变浓: %s" % ratios)
        for i in c2.find_all():
            b = c2.bbox(i)
            self.assertGreaterEqual(b[0], 0)
            self.assertLessEqual(b[2], 200)

        # 星座可复现：同 seed 两次坐标一致
        c3 = tk.Canvas(self.host, width=300, height=160, bg=bg,
                       highlightthickness=0)
        c4 = tk.Canvas(self.host, width=300, height=160, bg=bg,
                       highlightthickness=0)
        decor.constellation(c3, 15, 10, 285, 150, bg=bg, count=9, seed=7)
        decor.constellation(c4, 15, 10, 285, 150, bg=bg, count=9, seed=7)
        a = [c3.coords(i) for i in c3.find_all()]
        b = [c4.coords(i) for i in c4.find_all()]
        self.assertEqual(a, b)

    def test_hero_banner_stays_inside_and_layered(self):
        bg = "#0E0E12"
        banner = decor.hero_banner(self.host, 640, 220, bg=bg)
        self.assertEqual((int(banner["width"]), int(banner["height"])),
                         (640, 220))
        items = banner.find_all()
        self.assertGreater(len(items), 100, "组合画应有足量元素")
        kinds = {}
        for i in items:
            kinds[banner.type(i)] = kinds.get(banner.type(i), 0) + 1
        self.assertGreater(kinds.get("oval", 0), 20)
        self.assertGreaterEqual(kinds.get("line", 0), 6)
        for i in items:
            b = banner.bbox(i)
            if b:
                self.assertGreaterEqual(b[0], -1, "元素不能超出画布")
                self.assertGreaterEqual(b[1], -1)
                self.assertLessEqual(b[2], 641)
                self.assertLessEqual(b[3], 221)



