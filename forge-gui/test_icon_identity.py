"""Small, static regressions for vector icon identity and interaction states."""
import tkinter as tk
import unittest

from ui_icons import ICONS, IconCanvas


class IconIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("240x100")

    def tearDown(self):
        self.root.destroy()

    def test_overflow_and_warning_keep_small_size_specific_marks(self):
        self.assertTrue(all(kind == "dot" for kind, _coords in ICONS["more"]))
        self.assertEqual(ICONS["warning"][2][0], "dot")
        for name in ("more", "warning"):
            canvas = IconCanvas(self.root, name, size=16)
            canvas.pack()
            self.root.update_idletasks()
            self.assertTrue(canvas.find_withtag("icon"))

    def test_canvas_button_has_static_hover_focus_pressed_and_disabled_states(self):
        called = []
        icon = IconCanvas(self.root, "refresh", size=20, bg="#202028", command=lambda: called.append(True))
        icon.pack()
        self.root.update_idletasks()
        original = icon.cget("bg")
        icon._enter()
        self.assertNotEqual(icon.cget("bg"), original)
        icon._press()
        self.assertEqual(called, [True])
        icon._release()
        icon._focus_in()
        self.assertTrue(icon.find_withtag("focus"))
        icon.configure(state=tk.DISABLED)
        self.assertEqual(icon.cget("state"), tk.DISABLED)
        self.assertEqual(icon.cget("cursor"), "arrow")
        icon._press()
        self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
