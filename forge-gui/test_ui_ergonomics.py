"""Real Tk checks for reachable controls, keyboard use and transient feedback."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import chat_widgets as cw
import forge_gui_v2 as gui
import gui_theme as theme
import sub_agent as team
import secret_store
from model_picker import ModelPicker


def contrast(a, b):
    def luminance(color):
        parts = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        linear = [n / 12.92 if n <= .04045 else ((n + .055) / 1.055) ** 2.4 for n in parts]
        return sum(n * weight for n, weight in zip(linear, (.2126, .7152, .0722)))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + .05) / (low + .05)


class ErgonomicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="forge-ergonomics-")
        self.home = Path(self.tmp.name)
        self.stack = ExitStack()
        for obj, name, kwargs in (
            (gui, "DEFAULT_FORGE_HOME", {"new": self.home}),
            (gui, "_desktop_config_path", {"return_value": self.home / "desktop.json"}),
            (gui, "_find_run_py", {"return_value": None}),
            (secret_store, "SECRETS_FILE", {"new": self.home / "secrets.json"}),
            (team, "config_path", {"return_value": self.home / "team.json"}),
            (gui, "_autostart_enabled", {"return_value": False}),
            (gui.ForgeGuiApp, "_start_sysmon", {}),
            (gui.ForgeGuiApp, "_repo_root", {"return_value": self.home}),
            (Path, "home", {"return_value": self.home}),
        ):
            self.stack.enter_context(patch.object(obj, name, **kwargs))
        agents = self.home / ".openclaw-autoclaw" / "agents"
        for i in range(20):
            (agents / str(i)).mkdir(parents=True)
        self.root = tk.Tk()
        # Other roots in the DPI suite can retain a shared screen's scaling.
        # These are 100% interaction checks; five DPI scales have their own suite.
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.root.geometry("1360x850+20+20")

    def tearDown(self):
        if hasattr(self, "app"):
            self.app._closing = True
        for token in self.root.tk.call("after", "info"):
            self.root.after_cancel(token)
        self.root.destroy()
        self.stack.close()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def pump(self, seconds=.15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)

    def app_ui(self):
        self.app = gui.ForgeGuiApp(self.root)
        self.root.geometry("1360x850+20+20")
        self.pump()
        return self.app

    def test_essential_text_has_readable_contrast(self):
        for fg, bg in (("msg_user_fg", "msg_user_bg"), ("msg_agent_fg", "msg_agent_bg"),
                       ("placeholder", "input_bg"), ("muted", "surface")):
            self.assertGreaterEqual(contrast(theme.C[fg], theme.C[bg]), 4.5, (fg, bg))

    def test_composer_keyboard_send_and_single_stop_preserve_draft(self):
        calls = []
        card = cw.InputCard(self.root, on_send=lambda: calls.append("send"),
                            on_stop=lambda: calls.append("stop"))
        card.pack(fill=tk.X)
        self.pump()
        card.send_circle.event_generate("<Button-1>")
        self.assertEqual(calls, [])
        card.entry.insert("1.0", "真实输入")
        self.pump()
        card.send_circle.focus_force()
        self.pump()
        card.send_circle.event_generate("<Return>")
        self.assertEqual(calls, ["send"])
        card.set_busy(True)
        card.entry.delete("1.0", "end")
        card.entry.insert("1.0", "下一条草稿")
        self.pump()
        card.stop_circle.event_generate("<Button-1>")
        card.stop_circle.event_generate("<Button-1>")
        self.assertEqual(calls, ["send", "stop"])
        self.assertEqual(card.footer_left.cget("text"), "正在停止…")
        card.set_busy(False)
        self.assertEqual(card.send_var.get(), "下一条草稿")

    def test_tooltip_travel_and_destroy_leave_no_popup_or_timer_error(self):
        button = theme.glyph_button(self.root, "＋", lambda: None, tooltip="添加附件")
        button.pack()
        self.pump()
        button.event_generate("<Enter>")
        button.event_generate("<Leave>")
        self.pump(.5)
        self.assertFalse(any(isinstance(w, tk.Toplevel) for w in button.winfo_children()))
        button.event_generate("<Enter>")
        self.pump(.5)
        self.assertTrue(any(isinstance(w, tk.Toplevel) for w in button.winfo_children()))
        button.destroy()
        other = theme.glyph_button(self.root, "＋", lambda: None, tooltip="另一个提示")
        other.pack()
        self.pump()
        other.event_generate("<Enter>")
        other.destroy()
        self.pump(.5)

    def test_popover_arrow_keys_activate_the_focused_action(self):
        calls = []
        anchor = theme.glyph_button(self.root, "⋯", lambda: None)
        anchor.pack()
        anchor.focus_force()
        self.pump()
        pop = theme.show_popover_menu(anchor, [
            {"label": "第一项", "command": lambda: calls.append(1)},
            {"label": "第二项", "command": lambda: calls.append(2)},
        ])
        self.pump()
        self.root.focus_get().focus_force()
        self.root.focus_get().event_generate("<Down>")
        self.pump()
        self.root.focus_get().event_generate("<space>")
        self.assertEqual(calls, [2])
        self.assertFalse(pop.winfo_exists())

    def test_directory_disclosure_keeps_fixed_actions_visible(self):
        app = self.app_ui()
        self.assertFalse(app._agents_list_frame.winfo_ismapped())
        app._agents_toggle_btn.invoke()
        self.pump()
        self.assertTrue(app._agents_list_frame.winfo_ismapped())
        self.assertTrue(app.sidebar_toggle_btn.winfo_ismapped())
        self.assertLessEqual(app.sidebar_toggle_btn.winfo_rooty() + app.sidebar_toggle_btn.winfo_height(),
                             app.sidebar.winfo_rooty() + app.sidebar.winfo_height())
        self.assertGreater(app.history_area.winfo_height(), 50)
        app._toggle_sidebar()
        self.assertFalse(app._sidebar_visible)
        self.assertTrue(app.sidebar_reveal_btn.winfo_ismapped())
        app.sidebar_reveal_btn.invoke()
        self.assertTrue(app._sidebar_visible)

    def test_model_picker_opens_and_selects_with_keyboard(self):
        var = tk.StringVar(value="local-0")
        picker = ModelPicker(self.root, var, values=[f"local-{n}" for n in range(12)])
        picker.pack()
        picker.focus_force()
        self.pump()
        picker.event_generate("<Return>")
        self.pump()
        self.assertIsNotNone(picker._popup)
        self.root.focus_get().event_generate("<Down>")
        self.pump()
        first = self.root.focus_get()
        first.event_generate("<Down>")
        self.pump()
        self.root.focus_get().event_generate("<Return>")
        self.assertEqual(var.get(), "local-1")
        self.assertIsNone(picker._popup)

    def test_team_mode_and_failed_save_show_local_feedback(self):
        app = self.app_ui()
        app._team_subs_var.set(True)
        app._team_cluster_var.set(True)
        app._team_select_mode("cluster")
        self.assertFalse(app._team_subs_var.get())
        self.assertIn("未保存", app._team_feedback_var.get())
        with patch.object(team, "save_config", side_effect=OSError("disk full")):
            app._team_save()
        self.assertIn("保存失败", app._team_feedback_var.get())
        app._team_save()
        self.assertFalse(app._team_dirty)
        self.assertIn("已保存", app._team_feedback_var.get())
        app._show_view("agents")
        self.root.geometry("940x700")
        self.pump()
        self.assertTrue(app._team_save_btn.winfo_ismapped())
        self.assertLess(app._team_save_btn.winfo_rooty(), self.root.winfo_rooty()+self.root.winfo_height())

    def test_welcome_repeated_resize_keeps_all_real_actions_reachable(self):
        app = self.app_ui()
        self.assertEqual(app._chat_history, [])
        for _ in range(4):
            app._show_chat_start()
        self.root.geometry("940x700")
        self.pump()
        empty = app.chat_area._empty
        labels = []
        def walk(parent):
            for child in parent.winfo_children():
                if isinstance(child, tk.Label):
                    labels.append(child)
                walk(child)
        walk(empty)
        for text in ("开始对话", "交给 Forge 一个任务", "查看项目工作区"):
            label = next(w for w in labels if w.cget("text") == text)
            self.assertTrue(label.winfo_ismapped())
            self.assertLess(label.winfo_rooty()+label.winfo_height(), app.input_card.winfo_rooty())
        self.assertEqual(app._chat_history, [])


if __name__ == "__main__":
    unittest.main()
