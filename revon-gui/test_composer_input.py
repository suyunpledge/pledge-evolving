"""Input focus and native IME coordinates through real Tk layout changes."""
import ctypes
from ctypes import wintypes
import sys
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import chat_widgets as cw
from test_layout_dpi import pump


class ComposerInputTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("1000x640+120+80")
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.other = tk.Entry(self.root)
        self.other.pack()
        self.sent = []
        self.card = cw.InputCard(
            self.root, on_send=lambda: self.sent.append(self.card.send_var.get()),
            thinking_var=tk.StringVar(value="medium"),
            thinking_choices=(("low", "Low", ""), ("medium", "Medium", "")))
        # Fixed width inside a moving ancestor reproduces the native Tk cache
        # bug: the insertion point has the same Text-local coordinates.
        self.card.pack(side=tk.BOTTOM, pady=20)
        self.card.configure(width=650)
        pump(self.root)
        self.root.focus_force()
        self.card.entry.focus_set()
        pump(self.root)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def key(self, key):
        focused = self.root.focus_get()
        modifier, _, symbol = key.rpartition("-")
        prefix = modifier + "-" if modifier else ""
        focused.event_generate(f"<{prefix}KeyPress-{symbol}>")
        focused.event_generate(f"<{prefix}KeyRelease-{symbol}>")
        pump(self.root, .03)

    def test_ascii_editing_newlines_and_busy_draft(self):
        for key in ("a", "1", "b", "BackSpace", "Shift-Return", "2"):
            self.key(key)
        self.assertEqual(self.card.send_var.get(), "a1\n2")
        self.assertEqual(self.sent, [])
        self.card.set_busy(True)
        self.key("3")
        self.assertEqual(self.card.send_var.get(), "a1\n23")
        self.key("Return")
        self.assertEqual(self.sent, [])
        self.card.set_busy(False)
        self.key("Return")
        self.assertEqual(self.sent, ["a1\n23"])

    def test_clicking_composer_padding_accepts_keyboard_input(self):
        self.other.focus_set()
        pump(self.root)
        self.card._card.canvas.event_generate("<Button-1>", x=4, y=4)
        pump(self.root)
        self.assertIs(self.root.focus_get(), self.card.entry)
        self.key("7")
        self.assertEqual(self.card.send_var.get(), "7")
        self.assertEqual(self.other.get(), "")

    def test_escape_from_reasoning_popup_restores_typing(self):
        self.card._toggle_reasoning_popup()
        pump(self.root)
        popup = self.card._reasoning_popup
        popup.focus_force()
        pump(self.root)
        popup.event_generate("<Escape>")
        pump(self.root)
        self.assertIsNone(self.card._reasoning_popup)
        self.assertIs(self.root.focus_get(), self.card.entry)
        self.key("8")
        self.assertEqual(self.card.send_var.get(), "8")

    def test_dismissing_popup_does_not_steal_focus_from_another_field(self):
        self.card._toggle_reasoning_popup()
        pump(self.root)
        self.other.focus_set()
        pump(self.root)
        self.card._close_reasoning_popup()
        pump(self.root)
        self.assertIs(self.root.focus_get(), self.other)
        self.key("9")
        self.assertEqual(self.other.get(), "9")
        self.assertEqual(self.card.send_var.get(), "")

    @unittest.skipUnless(sys.platform == "win32", "Windows IMM coordinates")
    def test_native_composition_follows_maximize_restore_without_caret_edit(self):
        anchor = self.card._ime_anchor
        self.card.send_var.set("abc")
        pump(self.root)
        local_box = self.card.entry.bbox("insert")
        positions = []
        get_composition = anchor._imm32.ImmGetCompositionWindow
        get_composition.argtypes = [wintypes.HANDLE, ctypes.POINTER(anchor._COMPOSITIONFORM)]
        get_composition.restype = wintypes.BOOL
        for state in ("normal", "zoomed", "normal"):
            self.root.state(state)
            pump(self.root)
            self.assertEqual(self.card.entry.bbox("insert"), local_box)
            hwnd = self.root.winfo_id()
            context = anchor._imm32.ImmGetContext(hwnd)
            self.assertTrue(context)
            try:
                composition = anchor._COMPOSITIONFORM()
                self.assertTrue(get_composition(context, ctypes.byref(composition)))
            finally:
                anchor._imm32.ImmReleaseContext(hwnd, context)
            x, y, _w, _h = self.card.entry.bbox("insert")
            expected = (self.card.entry.winfo_rootx() - self.root.winfo_rootx() + x,
                        self.card.entry.winfo_rooty() - self.root.winfo_rooty() + y)
            actual = (composition.ptCurrentPos.x, composition.ptCurrentPos.y)
            self.assertEqual(actual, expected)
            positions.append(actual)
        self.assertNotEqual(positions[0], positions[1])
        self.assertEqual(positions[0], positions[2])

    @unittest.skipUnless(sys.platform == "win32", "Windows IMM lifecycle")
    def test_no_updates_from_unfocused_composer_and_no_leaked_binding(self):
        anchor = self.card._ime_anchor
        self.other.focus_set()
        pump(self.root)
        with patch.object(anchor, "_anchor_func") as update:
            anchor._reanchor()
            pump(self.root)
            update.assert_not_called()
        tokens = [token for w, seq, token in anchor._bindings if w is self.root]
        self.card.destroy()
        pump(self.root)
        for token in tokens:
            self.assertNotIn(token, self.root.bind("<Configure>"))

    @unittest.skipUnless(sys.platform == "win32", "Windows IMM lifecycle")
    def test_native_context_is_released_even_when_positioning_fails(self):
        anchor = self.card._ime_anchor
        anchor._caret = (self.root.winfo_id(), 25, 40, 20)
        imm = Mock()
        imm.ImmGetContext.return_value = 123
        imm.ImmSetCompositionWindow.side_effect = OSError("IME unavailable")
        with patch.object(anchor, "_imm32", imm):
            with self.assertRaises(OSError):
                anchor._imm_set()
        imm.ImmReleaseContext.assert_called_once_with(self.root.winfo_id(), 123)


if __name__ == "__main__":
    unittest.main()
