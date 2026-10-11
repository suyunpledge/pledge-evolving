"""Focused real-Tk checks for the static Forge hero motif."""
import tkinter as tk
import unittest

import decor


class ForgeDecorCanvasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def test_hero_sizes_stay_in_canvas_without_scheduling_callbacks(self):
        for width, height in ((480, 112), (640, 220), (96, 40)):
            with self.subTest(size=(width, height)):
                canvas = decor.hero_banner(
                    self.root, width, height, bg="#171923",
                    accent="#5955D9", accent2="#A19BFF", spark="#F5BC74",
                )
                canvas.pack()
                self.root.update_idletasks()
                self.assertEqual(canvas.winfo_reqwidth(), width)
                self.assertEqual(canvas.winfo_reqheight(), height)
                self.assertEqual(len(canvas.find_all()), 25)

                for item in canvas.find_all():
                    box = canvas.bbox(item)
                    self.assertIsNotNone(box)
                    x1, y1, x2, y2 = box
                    self.assertGreaterEqual(x1, 0, (width, height, item, box))
                    self.assertGreaterEqual(y1, 0, (width, height, item, box))
                    self.assertLessEqual(x2, width, (width, height, item, box))
                    self.assertLessEqual(y2, height, (width, height, item, box))

                # Decorations are synchronous Canvas items and must not leave
                # Tk timer callbacks behind.
                self.assertEqual(tuple(self.root.tk.call("after", "info")), ())
                canvas.destroy()


if __name__ == "__main__":
    unittest.main()
