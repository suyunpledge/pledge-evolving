"""Navigation, disclosure and icon hit-area contracts for the workbench."""
import tkinter as tk
import unittest

import gui_theme as theme
from test_layout_dpi import isolated_app, pump
from ui_icons import IconCanvas


class LayoutStructureTests(unittest.TestCase):
    def test_task_entry_preserves_chat_draft_and_navigation_groups(self):
        with isolated_app() as (root, app, errors):
            app.send_var.set('Keep this draft')
            app.sidebar_shortcuts['task'].invoke()
            self.assertEqual(app._active_view, 'task')
            app._return_to_chat()
            pump(root)
            self.assertEqual(app.send_var.get(), 'Keep this draft')
            self.assertGreater(app.history_area.winfo_height(), 100)
            for key in ('evolution', 'config'):
                self.assertTrue(app.sidebar_navigation[key].winfo_ismapped())
                self.assertGreater(app.sidebar_navigation[key].winfo_rooty(),
                                   app.history_area.winfo_rooty())
            self.assertFalse(errors)

    def test_options_disclosure_retains_widgets_and_reports_active_settings(self):
        with isolated_app() as (root, app, errors):
            card = app.input_card
            app.send_var.set('Never send during layout changes')
            team = card.team_pill
            self.assertFalse(card._options_controls.winfo_ismapped())
            card.options_btn.invoke()
            pump(root)
            for widget in (team, card.team_settings_btn, card.project_files_btn, card.planning_pill):
                self.assertTrue(widget.winfo_ismapped())
            team.set_mode('on')
            card.set_planning_active(True)
            card.options_btn.invoke()
            pump(root)
            self.assertFalse(card._options_controls.winfo_ismapped())
            self.assertEqual(card.options_btn.cget('fg'), theme.C['accent_text'])
            self.assertEqual(app.send_var.get(), 'Never send during layout changes')
            card.options_btn.invoke()
            pump(root)
            self.assertIs(card.team_pill, team)
            self.assertEqual(team.mode(), 'on')
            self.assertFalse(errors)

    def test_small_icon_has_larger_centered_keyboard_reachable_hit_area(self):
        root = tk.Tk()
        root.withdraw()
        calls = []
        try:
            icon = IconCanvas(root, 'search', size=16, hit_size=32,
                              command=lambda: calls.append(True))
            self.assertEqual(int(icon.cget('width')), 32)
            item = icon.find_withtag('icon')[0]
            self.assertEqual(icon.coords(item), [8.0, 8.0])
            icon._activate()
            self.assertEqual(calls, [True])
            icon.configure(state=tk.DISABLED)
            icon._activate()
            self.assertEqual(calls, [True])
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
