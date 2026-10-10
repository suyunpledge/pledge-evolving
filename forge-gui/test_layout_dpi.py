"""Actual Tk layout at five font/DPI scales; never alter Windows display settings."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import subprocess
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
import unittest
from unittest.mock import patch

import chat_widgets as cw
import forge_gui_v2 as gui
import gui_theme as theme
import secret_store
import sub_agent
import workspace

SCALES = (1, 1.25, 1.5, 1.75, 2)
SIZES = ((1920, 1080), (2560, 1600), (960, 900), (1280, 800), gui.MIN_SIZE)


def pump(root, seconds=.15):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        root.update()
        time.sleep(.005)


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@contextmanager
def isolated_app(scale=1, size=(1440, 900), *, repo=None):
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        home = Path(tmp)
        for obj, name, opts in (
            (gui, "DEFAULT_FORGE_HOME", {"new": home}),
            (gui, "_desktop_config_path", {"return_value": home / "desktop.json"}),
            (gui, "_find_run_py", {"return_value": repo / "run.py" if repo else None}),
            (gui.ForgeGuiApp, "_repo_root", {"return_value": repo or home}),
            (gui, "_autostart_enabled", {"return_value": False}),
            (gui.ForgeGuiApp, "_start_sysmon", {}),
            (secret_store, "SECRETS_FILE", {"new": home / "secrets.json"}),
            (sub_agent, "config_path", {"return_value": home / "team.json"}),
            (Path, "home", {"return_value": home}),
        ):
            stack.enter_context(patch.object(obj, name, **opts))
        gui.save_user_layer(home, [{"id": "mimo", "config": {
            "wire": "openai", "baseURL": "https://example.invalid/v1", "model": "mimo-v2.6-flash-long-model-name-for-layout-testing", "apiKey": ""}}])
        root = tk.Tk()
        root.attributes("-alpha", 0)
        root.tk.call("tk", "scaling", scale * 96 / 72)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        app = gui.ForgeGuiApp(root)
        app.model_var.set("mimo-v2.6-flash-long-model-name-for-layout-testing")
        root.maxsize(10000, 10000)
        root.geometry(f"{size[0]}x{size[1]}+0+0")
        pump(root)
        try:
            yield root, app, errors
        finally:
            app._closing = True
            owners = [root, *descendants(root)]
            for token in root.tk.call("after", "info"):
                command = root.tk.call("after", "info", token)[0]
                owner = next((widget for widget in owners if command in (widget._tclCommands or [])), root)
                owner.after_cancel(token)
            root.destroy()
            # File-backed workers must quiesce before Windows removes this
            # temporary home; joins happen after Tk has been destroyed.
            app.desktop_features.thread.join(3)
            for job in app._background_jobs.values():
                worker=job.get('thread')
                if worker is not None: worker.join(3)


class DpiLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def assert_inside(self, widget, ancestor):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        left, top = widget.winfo_rootx(), widget.winfo_rooty()
        self.assertGreaterEqual(left, ancestor.winfo_rootx(), str(widget))
        self.assertGreaterEqual(top, ancestor.winfo_rooty(), str(widget))
        self.assertLessEqual(left+widget.winfo_width(), ancestor.winfo_rootx()+ancestor.winfo_width(), str(widget))
        self.assertLessEqual(top+widget.winfo_height(), ancestor.winfo_rooty()+ancestor.winfo_height(), str(widget))

    def assert_all_ink_inside(self, text, message):
        end = text.index("end-1c")
        index = "1.0"
        while text.compare(index, "<", end):
            char = text.get(index, f"{index}+1c")
            if char != "\n":
                box = text.bbox(index)
                self.assertIsNotNone(box, (index, text.winfo_height()))
                x, y, width, height = box
                self.assertLessEqual(x+width, text.winfo_width())
                self.assertLessEqual(y+height, text.winfo_height())
                ancestor = text.master
                while ancestor is not message.master:
                    ax, ay = text.winfo_rootx()+x, text.winfo_rooty()+y
                    self.assertGreaterEqual(ax, ancestor.winfo_rootx())
                    self.assertGreaterEqual(ay, ancestor.winfo_rooty())
                    self.assertLessEqual(ax+width, ancestor.winfo_rootx()+ancestor.winfo_width())
                    self.assertLessEqual(ay+height, ancestor.winfo_rooty()+ancestor.winfo_height())
                    ancestor = ancestor.master
            index = text.index(f"{index}+1c")

    def test_five_dpi_scales_and_five_window_sizes(self):
        metrics = {}
        for scale in SCALES:
            for size in SIZES:
                with self.subTest(scale=scale, size=size), isolated_app(scale, size) as (root, app, errors):
                    card = app.input_card
                    metrics[scale] = tkfont.Font(root=root, font=theme.FONT_UI).metrics("linespace")
                    for widget in (card.entry, card.plus, card.model_widget, card.mode_pill, card.provider_label, card.send_circle):
                        self.assert_inside(widget, card._card._cv)
                    self.assertLess(card.entry.winfo_rooty(), card._tools_row.winfo_rooty())
                    self.assertLess(card._tools_row.winfo_rooty(), card._toolbar.winfo_rooty())
                    self.assertEqual(card.model_widget.get(), app.model_var.get())
                    self.assertIn("long-model-name", card.model_widget._shown_text)
                    card.set_busy(True)
                    pump(root, .03)
                    self.assert_inside(card.stop_circle, card._card._cv)
                    card.set_busy(False)
                    self.assertFalse(errors, errors)
        self.assertGreater(metrics[2], metrics[1]*1.7, "the test must actually change font metrics")

    def test_message_emoji_pixels_survive_resize_at_all_scales(self):
        original = "中文 mixed-text 😀 收尾🎉"
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, (1360, 950)) as (root, app, errors):
                app.chat_area.clear()
                user = app.chat_area.add_user(original)
                agent = app.chat_area.add_agent()
                agent.render_markdown(original)
                for size in ((1920, 1080), (940, 700), (1280, 800)):
                    root.geometry(f"{size[0]}x{size[1]}")
                    pump(root)
                    for message in (user, agent):
                        for text in descendants(message):
                            if isinstance(text, cw.InlineText):
                                self.assertEqual(text.display_text(), original)
                                self.assert_all_ink_inside(text, message)
                self.assertFalse(errors, errors)

    def test_long_draft_keeps_timeline_and_controls_at_minimum_window(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, gui.MIN_SIZE) as (root, app, errors):
                app.send_var.set("真实输入布局测试\n" * 30)
                app.input_card._resize_entry()
                pump(root)
                self.assert_inside(app.input_card.send_circle, app.input_card._card._cv)
                self.assertGreaterEqual(app.chat_area.winfo_height(), theme.ui_px(root, 80))
                self.assertFalse(errors, errors)

    def test_long_code_has_horizontal_and_vertical_scroll_access(self):
        with isolated_app(1.5, gui.MIN_SIZE) as (root, app, errors):
            app.chat_area.clear()
            reply = app.chat_area.add_agent()
            reply.render_markdown("```python\n" + "x = '" + "long-code" * 100 + "'\n" + "pass\n" * 30 + "```\n")
            pump(root)
            code = next(widget for widget in descendants(reply) if isinstance(widget, tk.Text) and not isinstance(widget, cw.InlineText))
            self.assertTrue(code.cget("xscrollcommand"))
            self.assertTrue(code.cget("yscrollcommand"))
            code.xview_moveto(1)
            pump(root)
            self.assertGreater(code.xview()[0], 0)
            code.yview_moveto(1)
            pump(root)
            self.assertGreater(code.yview()[0], 0)
            self.assertFalse(errors, errors)

    def test_workspace_toggle_recalculates_content_width_and_heading_wrap(self):
        with isolated_app(1.5, (1600, 950)) as (root, app, errors):
            app.chat_area.clear()
            user = app.chat_area.add_user("长消息用于窗口与工作区宽度测试 " * 8 + "😀🎉")
            reply = app.chat_area.add_agent()
            reply.render_markdown("# Heading follows its parent width\n\n实际正文 " * 5 + "🎉")
            pump(root)
            before = int(user.label.cget("wraplength"))
            app._open_workspace("file_tree")
            pump(root, .3)
            self.assertLessEqual(int(user.label.cget("wraplength")), before)
            for text in descendants(reply):
                if isinstance(text, cw.InlineText):
                    self.assert_all_ink_inside(text, reply)
            app._close_workspace()
            pump(root)
            self.assertFalse(errors, errors)

    def test_short_reply_has_bounded_body_and_explicit_workspace_button(self):
        with isolated_app(1, (1600, 950)) as (root, app, errors):
            app.chat_area.clear()
            reply = app.chat_area.add_agent()
            reply.render_markdown("实际短回复 🎉")
            called = []
            actions = reply.add_actions([{"label": "打开工作区", "command": lambda: called.append(1)}])
            pump(root)
            self.assertLess(reply._bubble.winfo_width(), reply._bubble_host.winfo_width()*.65)
            button = actions._buttons[0]
            self.assertTrue(button.cget("image"))
            self.assertLess(button.winfo_width(), actions.winfo_width())
            button.invoke()
            self.assertEqual(called, [1])
            self.assertFalse(errors, errors)

    def test_actual_git_brief_is_not_chat_history_and_disappears_for_long_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "capture_output": True, "check": True}
            subprocess.run(["git", "init", str(repo)], **flags)
            (repo / "run.py").write_text("# real temporary repository\n", encoding="utf-8")
            with isolated_app(1, (1600, 1080), repo=repo) as (root, app, errors):
                deadline = time.monotonic()+3
                while "1 个文件" not in app.chat_area._context_var.get() and time.monotonic() < deadline:
                    pump(root, .05)
                self.assertIn("1 个文件", app.chat_area._context_var.get())
                app.chat_area.add_user("actual question")
                reply = app.chat_area.add_agent()
                reply.render_markdown("actual answer")
                pump(root)
                self.assertTrue(app.chat_area._context_hint.winfo_ismapped())
                self.assertEqual(app._chat_history, [])
                app.chat_area._context_hint.winfo_children()[1]._buttons[0].invoke()
                self.assertIn("实际更改", app.send_var.get())
                self.assertEqual(app._chat_history, [])
                app.chat_area.add_user("next actual question")
                pump(root)
                self.assertFalse(app.chat_area._context_hint.winfo_ismapped())
                self.assertFalse(errors, errors)

    def test_sash_drag_is_preserved_after_refresh_and_resize(self):
        with isolated_app(1, (1600, 950)) as (root, app, errors):
            app._open_workspace("file_tree")
            pump(root, .3)
            panel = app.workspace
            x, y = panel._vp.sash_coord(0)
            panel._vp.event_generate("<ButtonPress-1>", x=x+20, y=y+2)
            self.assertTrue(panel._sash_dragging)
            panel._vp.event_generate("<B1-Motion>", x=x+20, y=y+55)
            panel._vp.event_generate("<ButtonRelease-1>", x=x+20, y=y+55)
            pump(root)
            chosen = panel._vp.sash_coord(0)[1]
            panel._place_sashes_once()
            self.assertEqual(panel._vp.sash_coord(0)[1], chosen)
            fractions = panel._user_sash_fractions
            root.geometry("1800x1080")
            pump(root, .3)
            self.assertEqual(panel._user_sash_fractions, fractions)
            self.assertAlmostEqual(panel._vp.sash_coord(0)[1]/panel._vp.winfo_height(), fractions[0], delta=.04)
            self.assertFalse(errors, errors)

    def test_diff_empty_state_is_actionable_and_selected_file_replaces_it(self):
        with isolated_app(1.25, (1600, 950)) as (root, app, errors):
            app._open_workspace("file_tree")
            pump(root)
            panel = app.workspace
            self.assertTrue(panel._diff_empty.winfo_ismapped())
            self.assertTrue(panel._diff_empty_actions._buttons[0].winfo_ismapped())
            path = panel._repo_root / "actual.py"
            path.write_text("print('actual')\n", encoding="utf-8")
            panel._is_git_repo = True
            panel._git_status = {"actual.py": "?"}
            panel._render_diff(path, loaded=("", "print('actual')\n"))
            pump(root)
            self.assertFalse(panel._diff_empty.winfo_ismapped())
            self.assertIn("actual", panel._diff_text.get("1.0", "end"))
            self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
