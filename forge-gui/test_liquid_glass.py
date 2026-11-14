"""Glass surfaces must preserve input, keyboard actions and bounded layout."""
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import chat_widgets as cw
import forge_gui_v2 as gui
import gui_theme as theme
import liquid_glass as glass
from test_layout_dpi import pump


class GlassInteractionTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.root.geometry("640x480+100+100")
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_painted_padding_keeps_the_owning_rows_click_action(self):
        row = tk.Frame(self.root, width=200, height=40)
        row.pack()
        backdrop = glass.GlassBackdrop(row, glass.chip_spec(theme.C["glass"], radius=15))
        calls = []
        row.bind("<Button-1>", lambda event: calls.append("activate"))
        pump(self.root)
        backdrop._label.event_generate("<Button-1>", x=3, y=3)
        self.assertEqual(calls, ["activate"])

    def test_unfold_collapse_and_destroy_clear_animation_jobs(self):
        panel = glass.GlassPanel(self.root, spec=glass.folder_spec(theme.C["chat"]))
        panel.pack(fill=tk.X)
        tk.Label(panel.content, text="步骤\n" * 8).pack()
        pump(self.root)
        panel.unfold()
        pump(self.root, .05)
        panel.fold()
        self.assertIsNone(panel._anim_job)
        self.assertEqual(panel.canvas.itemcget(panel._win, "state"), "normal")
        panel.unfold()
        pump(self.root, .4)
        self.assertEqual(panel.canvas.winfo_height(), panel.natural_height())
        panel.unfold()
        panel.destroy()
        pump(self.root, .4)

    def test_reduced_motion_and_long_folder_titles_fit_narrow_width(self):
        self.root.geometry("420x400")
        card = cw.ToolCard(self.root, title="很长的工具调用摘要 " * 12, rows=[{"name": "read_file"}])
        card.pack(fill=tk.X, padx=10)
        pump(self.root)
        right = card._summary_label.winfo_rootx() + card._summary_label.winfo_width()
        self.assertLessEqual(right, card.winfo_rootx() + card.winfo_width())
        with patch.dict("os.environ", FORGE_REDUCE_MOTION="1"):
            card.toggle()
            pump(self.root)
        self.assertIsNone(card._card._anim_job)
        self.assertTrue(card._card.content.winfo_ismapped())

    def test_destroying_glass_window_cancels_pending_render(self):
        popup = tk.Toplevel(self.root)
        host = glass.glass_window(popup, fill=theme.C["glass"], radius=18, matte=theme.C["bg"])
        tk.Label(host, text="待关闭菜单").pack()
        glass.fit_glass_window(host)
        host._glass_canvas.event_generate("<Configure>", width=200, height=120)
        job = host._glass_state["job"]
        self.assertIsNotNone(job)
        popup.destroy()
        self.assertNotIn(job, self.root.tk.call("after", "info"))
        pump(self.root)

    def test_destructive_modal_defaults_to_cancel_and_returns_input_focus(self):
        entry = tk.Entry(self.root)
        entry.pack()
        pump(self.root)  # Native focus requires the toplevel to be mapped first.
        self.root.focus_force()
        entry.focus_set()
        pump(self.root)
        seen = []

        def inspect():
            dialog = self.root.grab_current()
            button = dialog.focus_get()
            seen.append(str(button.cget("text")))
            button.event_generate("<Return>")

        self.root.after(250, inspect)
        result = gui.ForgeGuiApp._modal_dialog(
            SimpleNamespace(root=self.root), "Delete test", "Keep the draft on cancel.",
            [("Delete", True, "danger"), ("Cancel", False, "ghost")], tone="danger")
        pump(self.root)
        self.assertEqual(seen, ["Cancel"])
        self.assertIs(result, False)
        self.assertIs(self.root.focus_get(), entry)
        self.assertFalse(any(getattr(w, "_forge_scrim", False) for w in self.root.winfo_children()))


if __name__ == "__main__":
    unittest.main()
