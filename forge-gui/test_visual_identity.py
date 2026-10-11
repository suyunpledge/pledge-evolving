"""Visual polish must retain contrast and the existing single-action contract."""
import tkinter as tk
import unittest

import gui_theme as theme
from test_ui_ergonomics import contrast


class VisualIdentityTests(unittest.TestCase):
    def test_readable_text_and_action_colours(self):
        for foreground, background in (
            ("body", "surface"), ("muted", "surface"), ("ter", "sidebar"),
            ("placeholder", "input_bg"), ("accent_text", "sel"),
            ("error", "surface"), ("warn", "surface"), ("info", "surface"),
            ("msg_agent_fg", "msg_agent_bg"), ("msg_user_fg", "msg_user_bg"),
        ):
            self.assertGreaterEqual(contrast(theme.C[foreground], theme.C[background]), 4.5,
                                    (foreground, background))
        for background in ("accent", "accent_hover"):
            self.assertGreaterEqual(contrast("#FFFFFF", theme.C[background]), 4.5)
        self.assertGreaterEqual(contrast(theme.C["accent2"], theme.C["input_bg"]), 3)

    def test_circle_feedback_does_not_change_click_or_disabled_contract(self):
        root = tk.Tk()
        root.attributes("-alpha", 0)
        calls = []
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)
        try:
            button = theme.circle_button(root, "↑", lambda: calls.append("send"), size=36)
            button.pack()
            root.update()
            normal = button.itemcget(button._oval, "fill")
            button.event_generate("<Enter>")
            hover = button.itemcget(button._oval, "fill")
            self.assertNotEqual(normal, hover)
            button.event_generate("<Button-1>")
            self.assertNotEqual(button.itemcget(button._oval, "fill"), hover)
            button.event_generate("<ButtonRelease-1>")
            self.assertEqual(calls, ["send"])
            self.assertEqual(button.itemcget(button._oval, "fill"), hover)
            count = len(button.find_all())
            for _ in range(20):
                theme.circle_button_state(button, "muted", enabled=False)
                button.event_generate("<Enter>")
                button.event_generate("<Button-1>")
                button.event_generate("<Return>")
                button.event_generate("<space>")
                self.assertFalse(button._enabled)
                theme.circle_button_state(button, "primary", enabled=True)
            self.assertEqual(calls, ["send"])
            self.assertEqual(len(button.find_all()), count)
            self.assertEqual(button.itemcget(button._sheen, "state"), tk.NORMAL)
            self.assertEqual(errors, [])
        finally:
            root.destroy()

    def test_circle_decoration_stays_inside_at_five_dpi_scales(self):
        root = tk.Tk()
        root.attributes("-alpha", 0)
        original_scale = root.tk.call("tk", "scaling")
        try:
            for scale in (1, 1.25, 1.5, 1.75, 2):
                root.tk.call("tk", "scaling", scale * 96 / 72)
                size = theme.ui_px(root, 36)
                button = theme.circle_button(root, "↑", lambda: None, size=size)
                button.pack()
                root.update()
                for item in button.find_all():
                    bounds = button.bbox(item)
                    if bounds is not None:
                        self.assertGreaterEqual(min(bounds[:2]), 0)
                        self.assertLessEqual(max(bounds[2:]), size)
                button.destroy()
        finally:
            root.tk.call("tk", "scaling", original_scale)
            root.destroy()
