"""Shared button feedback must preserve native activation and runtime updates."""
import tkinter as tk
import unittest

import gui_theme as theme
from ui_icons import IconButton, icon_image
from test_layout_dpi import pump
from test_ui_ergonomics import contrast


class ControlFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root.configure(bg=theme.C["bg"])
        self.root.geometry("520x240")
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_primary_button_has_distinct_hover_press_and_disabled_feedback(self):
        calls = []
        button = theme.pill_button(self.root, "Run", lambda: calls.append(1), kind="primary")
        button.pack()
        pump(self.root)
        size = (button.winfo_reqwidth(), button.winfo_reqheight())
        normal = button.cget("bg")
        button.event_generate("<Enter>")
        hover = button.cget("bg")
        self.assertNotEqual(hover, normal)
        button.event_generate("<ButtonPress-1>", x=8, y=8)
        self.assertNotEqual(button.cget("bg"), hover)
        self.assertEqual(calls, [])  # native Button activates on release
        button.event_generate("<ButtonRelease-1>", x=8, y=8)
        self.assertEqual(calls, [1])
        button.configure(state=tk.DISABLED)
        self.assertNotIn(button.cget("bg"), (normal, hover))
        self.assertEqual(button.cget("cursor"), "arrow")
        self.assertEqual(int(button.cget("takefocus")), 0)
        button.invoke()
        button.event_generate("<Return>")
        self.assertEqual(calls, [1])
        button.configure(state=tk.NORMAL)
        button.event_generate("<Leave>")
        self.assertEqual(button.cget("bg"), normal)
        self.assertEqual(button.cget("cursor"), "hand2")
        self.assertEqual((button.winfo_reqwidth(), button.winfo_reqheight()), size)

    def test_disabled_icon_cannot_brighten_on_caption_or_colour_update(self):
        button = IconButton(self.root, icon="refresh", text="Retry", fg=theme.C["accent_text"],
                            disabledforeground=theme.C["muted"])
        button.configure(state=tk.DISABLED)
        muted = str(icon_image(button, "refresh", fg=theme.C["muted"]))
        self.assertEqual(str(button.cget("image")), muted)
        button.configure(text="Still waiting", fg=theme.C["error"])
        self.assertEqual(str(button.cget("image")), muted)
        button.configure(state=tk.NORMAL)
        self.assertEqual(str(button.cget("image")),
                         str(icon_image(button, "refresh", fg=theme.C["error"])))

    def test_keyboard_binding_keeps_existing_semantic_outline(self):
        control = tk.Frame(self.root, bg=theme.C["input_bg"], highlightthickness=1,
                           highlightbackground=theme.C["warn"])
        theme.bind_keyboard_action(control, lambda: None)
        self.assertEqual(control.cget("highlightbackground"), theme.C["warn"])

    def test_clickable_chip_is_keyboard_reachable(self):
        calls = []
        chip = theme.chip(self.root, "Changed", selected=True, command=lambda: calls.append(1))
        chip.pack()
        pump(self.root)
        chip.focus_force()
        pump(self.root)
        self.assertIs(self.root.focus_get(), chip)
        self.assertEqual(str(chip.cget("takefocus")), "1")
        chip.event_generate("<Return>")
        chip.event_generate("<space>")
        self.assertEqual(calls, [1, 1])

    def test_disabled_runtime_updates_restore_new_colours_on_enable(self):
        button = theme.pill_button(self.root, "Start", lambda: None, kind="primary")
        button.configure(state=tk.DISABLED)
        disabled_bg = button.cget("bg")
        button.configure(text="Restart", bg=theme.C["error_soft"], fg=theme.C["error"],
                         highlightbackground=theme.C["error"])
        self.assertEqual(button.cget("bg"), disabled_bg)
        button.configure(state=tk.NORMAL)
        self.assertEqual(button.cget("bg"), theme.C["error_soft"])
        self.assertEqual(button.cget("fg"), theme.C["error"])
        self.assertEqual(button.cget("highlightbackground"), theme.C["error"])

    def test_return_invokes_once_and_disabled_button_rejects_keyboard(self):
        calls = []
        button = theme.pill_button(self.root, "Review", lambda: calls.append(1))
        button.pack()
        pump(self.root)
        button.focus_force()
        pump(self.root)
        self.assertIs(self.root.focus_get(), button)
        button.event_generate("<Return>")
        self.assertEqual(calls, [1])
        button.event_generate("<space>")
        pump(self.root)
        self.assertEqual(calls, [1, 1])
        button.configure(state=tk.DISABLED)
        button.event_generate("<Return>")
        button.invoke()
        self.assertEqual(calls, [1, 1])
        button.destroy()
        pump(self.root)

    def test_dragging_out_cancels_native_mouse_action(self):
        calls = []
        button = theme.pill_button(self.root, "Remove", lambda: calls.append(1), kind="danger")
        button.pack()
        pump(self.root)
        button.event_generate("<Enter>")
        button.event_generate("<ButtonPress-1>", x=8, y=8)
        button.event_generate("<Leave>")
        button.event_generate("<ButtonRelease-1>", x=-15, y=-15)
        self.assertEqual(calls, [])
        self.assertEqual(button.cget("bg"), theme.C["surface2"])

    def test_action_colours_are_readable_in_all_feedback_states(self):
        for kind in ("primary", "ghost", "quiet", "danger", "accent_soft", "ok"):
            with self.subTest(kind=kind):
                button = theme.pill_button(self.root, "Action", lambda: None, kind=kind)
                button.pack()
                pump(self.root, .02)
                for event in (None, "<Enter>", "<ButtonPress-1>", "<ButtonRelease-1>"):
                    if event:
                        button.event_generate(event, x=8, y=8)
                    self.assertGreaterEqual(contrast(button.cget("fg"), button.cget("bg")), 4.5,
                                            (kind, event))
                button.configure(state=tk.DISABLED)
                self.assertGreaterEqual(contrast(button.cget("disabledforeground"), button.cget("bg")), 4.5)
                button.destroy()
        for background in ("accent", "accent_hover", "accent_pressed", "input_bg"):
            self.assertGreaterEqual(contrast(theme.C["focus_ring"], theme.C[background]), 3)

    def test_focus_and_semantic_outline_survive_caption_updates(self):
        button = theme.glyph_button(self.root, "Plan", lambda: None)
        button.configure(highlightbackground=theme.C["warn"], highlightcolor=theme.C["warn"])
        button.pack()
        pump(self.root)
        button.focus_force()
        pump(self.root)
        button.event_generate("<Enter>")
        button.configure(text="Planning: high")
        self.assertEqual(button.cget("highlightbackground"), theme.C["warn"])
        other = tk.Entry(self.root)
        other.pack()
        pump(self.root)
        other.focus_set()
        pump(self.root)
        self.assertEqual(button.cget("highlightbackground"), theme.C["warn"])
        theme.button_feedback(button, kind="quiet")
        button.event_generate("<Leave>")
        button.event_generate("<Enter>")
        self.assertEqual(button.cget("highlightbackground"), theme.C["warn"])

    def test_repeated_styling_does_not_accumulate_bindings_or_timers(self):
        button = theme.pill_button(self.root, "Review", lambda: None)
        button.pack()
        pump(self.root)
        enter = button.bind("<Enter>")
        pending = set(self.root.tk.call("after", "info"))
        for _ in range(20):
            theme.button_feedback(button)
            button.event_generate("<Enter>")
            button.event_generate("<Leave>")
        self.assertEqual(button.bind("<Enter>"), enter)
        self.assertEqual(set(self.root.tk.call("after", "info")), pending)

    def test_initial_disabled_icon_and_tk_colour_aliases_use_actual_state(self):
        button = IconButton(self.root, icon="refresh", text="Pending", state=tk.DISABLED,
                            foreground=theme.C["accent_text"], disabledforeground=theme.C["muted"])
        muted = str(icon_image(button, "refresh", fg=theme.C["muted"]))
        self.assertEqual(str(button.cget("image")), muted)
        button.configure({"foreground": theme.C["warn"], "background": theme.C["warn_soft"]})
        self.assertEqual(str(button.cget("image")), muted)
        button.configure(disabledforeground=theme.C["error"])
        self.assertEqual(str(button.cget("image")),
                         str(icon_image(button, "refresh", fg=theme.C["error"])))
        button.configure(state=tk.NORMAL)
        self.assertEqual(str(button.cget("image")),
                         str(icon_image(button, "refresh", fg=theme.C["warn"])))


if __name__ == "__main__":
    unittest.main()
