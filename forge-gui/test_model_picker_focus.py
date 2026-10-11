"""Model selection must leave a usable composer, without stealing other focus."""
import tkinter as tk
import threading
import unittest
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
from test_layout_dpi import isolated_app, pump


class ModelPickerFocusTests(unittest.TestCase):
    def prepare(self, root, app):
        app.user_rows = [
            {"id": name, "config": {"wire": "openai", "model": name,
             "baseURL": "https://example.invalid/v1", "apiKey": ""}}
            for name in ("model-a", "model-b")
        ]
        app.model_var.set("model-a")
        app.model_combo.configure(values=["model-a", "model-b"])
        app.send_var.set("未发送的草稿")
        root.focus_force()
        app.input_card.focus_entry()
        app.send_entry.mark_set("insert", "end-1c")
        pump(root)
        self.assertIs(root.focus_get(), app.send_entry)

    def assert_can_type(self, root, app):
        # Send a real Tk keyboard event to the actual focus owner. Inserting
        # directly into Text would pass even when the keyboard has no target.
        focused = root.focus_get()
        self.assertIs(focused, app.send_entry)
        self.assertEqual(str(focused.cget("state")), "normal")
        focused.event_generate("<KeyPress-x>", state=0)
        focused.event_generate("<KeyRelease-x>", state=0)
        pump(root)
        self.assertEqual(app.send_var.get(), "未发送的草稿x")
        self.assertIsNone(root.grab_current())

    def test_mouse_model_selection_returns_keyboard_to_composer(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            picker = app.model_combo
            picker.open_menu()
            pump(root)
            row = next(row for value, row in picker._rows if value == "model-b")
            row.event_generate("<Button-1>", x=12, y=12)
            pump(root)
            self.assertIsNone(picker._popup)
            self.assertEqual(app.model_var.get(), "model-b")
            self.assert_can_type(root, app)
            self.assertEqual(errors, [])

    def test_escape_returns_keyboard_to_original_editor(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            picker = app.model_combo
            picker.open_menu()
            pump(root)
            root.focus_get().event_generate("<Escape>")
            pump(root)
            self.assertIsNone(picker._popup)
            self.assertEqual(app.model_var.get(), "model-a")
            self.assert_can_type(root, app)
            self.assertEqual(errors, [])

    def test_keyboard_model_selection_returns_to_composer(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            picker = app.model_combo
            picker.focus_set()
            pump(root)
            picker.event_generate("<Return>")
            pump(root)
            root.focus_get().event_generate("<Down>")
            pump(root)
            root.focus_get().event_generate("<Down>")
            pump(root)
            root.focus_get().event_generate("<Return>")
            pump(root)
            self.assertEqual(app.model_var.get(), "model-b")
            self.assert_can_type(root, app)
            self.assertEqual(errors, [])

    def test_escape_from_keyboard_picker_preserves_navigation_focus(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            picker = app.model_combo
            picker.focus_set()
            pump(root)
            picker.event_generate("<Return>")
            pump(root)
            root.focus_get().event_generate("<Escape>")
            pump(root)
            self.assertIs(root.focus_get(), picker)
            self.assertEqual(app.send_var.get(), "未发送的草稿")
            self.assertEqual(errors, [])

    def test_model_change_during_reply_does_not_lock_draft(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            app._sending = True
            app.input_card.set_busy(True)
            picker = app.model_combo
            picker.open_menu()
            pump(root)
            with patch.object(app, "_restart_gateway") as restart:
                picker._select("model-b")
                pump(root)
                restart.assert_not_called()
                self.assert_can_type(root, app)
            self.assertEqual(errors, [])

    def test_slow_gateway_stop_keeps_keyboard_and_ui_responsive(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            proc = Mock()
            proc.poll.return_value = None
            proc.returncode = 0
            app.gateway_proc = proc
            app._gateway_provider = app.user_rows[0]["config"]
            entered, release, finished = (threading.Event() for _ in range(3))

            def slow_stop(target):
                self.assertIs(target, proc)
                entered.set()
                release.wait(3)
                proc.poll.return_value = 0
                finished.set()

            with patch.object(gui, "kill_process_tree", side_effect=slow_stop), \
                 patch.object(app, "_start_gateway", return_value=False) as start:
                try:
                    picker = app.model_combo
                    picker.open_menu()
                    pump(root)
                    picker._select("model-b")
                    self.assertTrue(entered.wait(.5))
                    pump(root)
                    self.assertTrue(app._restart_pending)
                    self.assert_can_type(root, app)
                    start.assert_not_called()
                finally:
                    release.set()
                    self.assertTrue(finished.wait(1))
                    pump(root, .5)
                start.assert_called_once()
                self.assertEqual(app.model_var.get(), "model-b")
                self.assertIs(root.focus_get(), app.send_entry)
            self.assertEqual(errors, [])

    def test_outside_click_keeps_other_editor_focus(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            other = tk.Entry(root)
            other.place(x=600, y=40, width=160)
            pump(root)
            picker = app.model_combo
            picker.open_menu()
            pump(root)
            other.focus_set()
            other.event_generate("<Button-1>", x=5, y=5)
            pump(root)
            self.assertIsNone(picker._popup)
            self.assertIs(root.focus_get(), other)
            other.event_generate("<KeyPress-x>", state=0)
            self.assertEqual(other.get(), "x")
            self.assertEqual(app.send_var.get(), "未发送的草稿")
            self.assertEqual(errors, [])

    def test_background_dismiss_never_requests_input_focus(self):
        with isolated_app() as (root, app, errors):
            self.prepare(root, app)
            picker = app.model_combo
            picker.open_menu()
            pump(root)
            with patch.object(root, "focus_displayof", return_value=None), \
                 patch.object(app.send_entry, "focus_set") as focus, \
                 patch.object(app.send_entry, "focus_force") as force:
                picker._dismiss_if_background()
                focus.assert_not_called()
                force.assert_not_called()
            self.assertIsNone(picker._popup)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
