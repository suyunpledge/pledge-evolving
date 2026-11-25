"""2026-11 代码审查修复的回归：命令面板、确认弹窗焦点、会话列表、模型显示名。

只建被测的那一个控件 / 调那一个方法，不启动整个应用。
"""
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import revon_gui_v2 as gui
import i18n
from command_palette import MAX_ROWS, CommandPalette, PaletteItem, balance, rank


def make_root(attempts=5):
    """本机 Tk 启动偶发读不到自带的 .tcl 脚本（与被测代码无关）；重试几次再放弃。"""
    for attempt in range(attempts):
        try:
            return tk.Tk()
        except tk.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(.2)


def pump(root, seconds=.15):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(.01)


def items(commands=17, sessions=5):
    noop = lambda: None  # noqa: E731
    return ([PaletteItem(f"命令{index}", "命令", noop) for index in range(commands)]
            + [PaletteItem(f"会话{index}", "会话", noop) for index in range(sessions)])


class PaletteBalanceTests(unittest.TestCase):
    def test_mixed_view_keeps_room_for_sessions(self):
        ranked, scope, _query = rank(items(), "")
        shown, totals = balance(ranked, scope)
        self.assertEqual([item.group for item in shown], ["命令"] * 6 + ["会话"] * 3)
        self.assertEqual(totals, {"命令": 17, "会话": 5})

    def test_unused_session_rows_go_back_to_commands(self):
        ranked, scope, _query = rank(items(sessions=1), "")
        shown, _totals = balance(ranked, scope)
        self.assertEqual([item.group for item in shown].count("命令"), MAX_ROWS - 1)
        self.assertEqual(shown[-1].group, "会话")

    def test_scoped_or_single_group_results_are_not_truncated(self):
        ranked, scope, _query = rank(items(), ">")
        self.assertEqual(len(balance(ranked, scope)[0]), 17)
        ranked, scope, _query = rank(items(sessions=0), "")
        self.assertEqual(len(balance(ranked, scope)[0]), 17)

    def test_rank_matches_the_text_the_caller_says_is_on_screen(self):
        entries = [PaletteItem(i18n.tr("展开 / 收起侧边栏"), "命令", lambda: None, keywords="sidebar")]
        self.assertEqual(rank(entries, "collapse")[0], [])
        english = i18n.Translator("en")
        shown = lambda item: f"{english.render(item.label)} {item.label} {item.keywords}"  # noqa: E731
        self.assertEqual(rank(entries, "collapse", text_of=shown)[0], entries)
        self.assertEqual(rank(entries, "收起", text_of=shown)[0], entries, "中文源串仍可搜到")


class PaletteWidgetTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.root.geometry("900x700+40+40")
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def open(self, entries):
        palette = CommandPalette(self.root, entries)
        self.addCleanup(palette.close)
        pump(self.root)
        return palette

    def test_sessions_are_visible_on_open(self):
        palette = self.open(items())
        groups = [row[4].group for row in palette._rows]
        self.assertEqual(groups.count("会话"), 3)
        self.assertEqual(len(groups), MAX_ROWS)

    def test_scoped_list_scrolls_to_every_item(self):
        palette = self.open(items())
        palette.query.set(">")
        pump(self.root)
        self.assertEqual(len(palette._results), 17)
        for _ in range(16):
            palette._move(1)
        self.assertEqual(palette._selected, 16)
        visible = [row[5] for row in palette._rows]
        self.assertIn(16, visible, "选中行必须在可见窗口里")
        self.assertEqual(len(visible), MAX_ROWS)
        self.assertEqual(palette._position.cget("text"), "17 / 17")
        palette._move(1)                      # 末尾再往下回到第一条
        self.assertEqual((palette._selected, palette._offset), (0, 0))

    def test_wheel_keeps_the_selection_inside_the_window(self):
        palette = self.open(items())
        palette.query.set(">")
        pump(self.root)
        palette._wheel(SimpleNamespace(delta=-120))
        self.assertEqual(palette._offset, 1)
        self.assertGreaterEqual(palette._selected, palette._offset)
        for _ in range(40):
            palette._wheel(SimpleNamespace(delta=-120))
        self.assertEqual(palette._offset, 17 - MAX_ROWS)
        ran = []
        palette._results[palette._selected] = PaletteItem("x", "命令", lambda: ran.append(1))
        palette._run_selected()
        self.assertEqual(ran, [1])

    def test_search_uses_the_displayed_language(self):
        self.root._keystone_locale = i18n.Translator("en")
        entries = [PaletteItem(i18n.tr("展开 / 收起侧边栏"), "命令", lambda: None, keywords="sidebar"),
                   PaletteItem(i18n.tr("知识库"), "命令", lambda: None)]
        palette = self.open(entries)
        palette.query.set("collapse")
        pump(self.root)
        self.assertEqual([str(item.label) for item in palette._results], ["展开 / 收起侧边栏"])
        palette.query.set("knowl")
        pump(self.root)
        self.assertEqual([str(item.label) for item in palette._results], ["知识库"])


class ModalFocusTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.root.geometry("700x500+40+40")
        self.root.update()
        self.app = SimpleNamespace(root=self.root)
        self.app._modal_dialog = lambda *a, **k: gui.RevonGuiApp._modal_dialog(self.app, *a, **k)

    def tearDown(self):
        self.root.destroy()

    def ask(self, call, *, press_space=True):
        seen = {}

        deadline = [40]

        def inspect():
            dialog = self.root.grab_current()
            focus = dialog.focus_lastfor() if dialog is not None else None
            # 进程里第一次显示中文要加载字体（约 0.2s），弹窗不一定在固定
            # 时刻已映射：等它把焦点交给按钮，最多 2s，超时再按当下状态判定。
            if (focus is None or focus is dialog) and deadline[0] > 0:
                deadline[0] -= 1
                self.root.after(50, inspect)
                return
            # 弹窗按钮是玻璃胶囊（GlassButton：可聚焦的 Label + invoke），
            # 认「能被激活的控件」而不是原生 Button 类名。
            actionable = callable(getattr(focus, "invoke", None)) and focus.winfo_class() in ("Button", "Label")
            seen["text"] = focus.cget("text") if actionable else None
            if press_space and actionable:
                focus.event_generate("<space>")   # 空格在聚焦按钮上的效果
            if dialog.winfo_exists():
                dialog.destroy()
        self.root.after(300, inspect)
        return call(), seen.get("text")

    def test_destructive_confirm_focuses_the_safe_button(self):
        result, focused = self.ask(lambda: gui.RevonGuiApp._confirm_discard_dialog(self.app))
        self.assertEqual(str(focused), "取消")
        self.assertFalse(result, "默认焦点上按空格不能放弃修改")
        result, focused = self.ask(lambda: gui.RevonGuiApp._confirm_dialog(self.app, "删除", "确定删除？", "删除"))
        self.assertEqual(str(focused), "取消")
        self.assertFalse(result)

    def test_failed_dialog_build_never_leaves_the_scrim_over_the_window(self):
        import liquid_glass as glass
        scrims = []
        real_scrim = glass.modal_scrim

        def tracked(*args, **kwargs):
            scrims.append(real_scrim(*args, **kwargs))
            return scrims[-1]

        def broken(*_args, **_kwargs):
            raise tk.TclError("render failed")

        with patch.object(glass, "modal_scrim", tracked), patch.object(glass, "glass_button", broken):
            with self.assertRaises(tk.TclError):
                gui.RevonGuiApp._confirm_dialog(self.app, "删除", "确定删除？", "删除")
        self.root.update()
        self.assertEqual(len(scrims), 1)
        self.assertFalse(scrims[0].winfo_exists(), "压暗层必须随失败的弹窗一起拆掉")
        self.assertFalse([w for w in self.root.winfo_children()
                          if isinstance(w, tk.Toplevel) and w.winfo_exists()])

    def test_non_destructive_dialog_still_focuses_its_primary_action(self):
        result, focused = self.ask(lambda: gui.RevonGuiApp._confirm_dialog(
            self.app, "应用", "应用这些修改？", "应用", danger=False))
        self.assertEqual(str(focused), "应用")
        self.assertTrue(result)


class HistoryRowTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.box = tk.Frame(self.root, bg="#000000")
        self.box.pack(fill=tk.BOTH, expand=True)
        app = gui.RevonGuiApp
        self.app = SimpleNamespace(_sending=False, _load_session=lambda sid: None,
                                   _session_snippet=app._session_snippet, _paint_history=app._paint_history)
        self.app._sync_history_running = lambda: app._sync_history_running(self.app)
        session = {"id": "s1", "title": "缓存优化", "messages": []}
        self.row = app._history_row(self.app, self.box, "#000000", session, "10:00", active=True)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_running_dot_follows_sending_state_in_place(self):
        self.assertIsNone(getattr(self.row, "_running_dot", None), "空闲时没有圆点")
        self.app._sending = True
        self.app._sync_history_running()
        dot = self.row._running_dot
        self.assertTrue(dot.winfo_exists())
        self.app._sync_history_running()
        self.assertIs(self.row._running_dot, dot, "重复同步不叠加圆点")
        self.app._sending = False            # 成功、失败、取消都走 _send_finished
        self.app._sync_history_running()
        self.assertIsNone(self.row._running_dot)
        self.assertFalse(dot.winfo_exists())

    def test_sync_survives_a_rebuilt_list(self):
        self.row.destroy()
        self.app._sending = True
        self.app._sync_history_running()     # 行已销毁：不抛错，引用被清掉
        self.assertIsNone(self.app._history_active)


class ModelLabelTests(unittest.TestCase):
    def edit(self, config, new_model):
        app = SimpleNamespace(
            _model_edit_row_id="p", home=None, user_rows=[{"id": "p", "config": dict(config)}],
            model_edit_var=SimpleNamespace(get=lambda: new_model),
            _refresh_provider_list=lambda: None, _sync_model_editor=lambda row: None,
            _set_status=lambda *a, **k: None)
        with patch.object(gui, "save_user_layer"):
            gui.RevonGuiApp._apply_model_edit(app)
        return app.user_rows[0]["config"]

    def test_changing_the_model_drops_the_old_display_name(self):
        config = self.edit({"baseURL": "http://x", "model": "glm-5.3-id", "modelLabel": "GLM5.3",
                            "smallModel": "GLM5.3"}, "kimi-k3")
        self.assertNotIn("modelLabel", config)
        self.assertEqual((config["model"], config["smallModel"]), ("kimi-k3", "kimi-k3"))

    def test_reapplying_the_same_model_keeps_the_display_name_and_small_model(self):
        config = self.edit({"baseURL": "http://x", "model": "glm-5.3-id", "modelLabel": "GLM5.3",
                            "smallModel": "glm-flash"}, "glm-5.3-id")
        self.assertEqual(config["modelLabel"], "GLM5.3")
        self.assertEqual(config["smallModel"], "glm-flash", "用户单独选的小模型不被覆盖")


if __name__ == "__main__":
    unittest.main()
