"""Dark surface coverage and real widget contracts, independent of screenshots."""
import ast
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk
import unittest

import gui_theme as theme
from development_mode import test_app
from test_ui_ergonomics import contrast


class ThemeCoverageTests(unittest.TestCase):
    def test_base_elements_popups_and_selected_controls_are_dark(self):
        root = tk.Tk()
        root.withdraw()
        try:
            style = theme.apply_ttk_theme(root)
            for name in ('TLabel', 'TFrame', 'TLabelframe', 'TLabelframe.Label', 'TNotebook',
                         'TButton', 'TCheckbutton', 'TMenubutton', 'Card.TFrame', 'Card.TLabel'):
                colour = style.lookup(name, 'background')
                self.assertTrue(colour.startswith('#'), (name, colour))
                self.assertLess(max(int(colour[i:i + 2], 16) for i in (1, 3, 5)), 90, name)
            popup = tk.Toplevel(root)
            label = tk.Label(popup, text='Dialog label')
            self.assertEqual(popup.cget('background'), theme.C['bg'])
            self.assertEqual(label.cget('background'), theme.C['bg'])
            self.assertEqual(label.cget('foreground'), theme.C['body'])
            for cls in (tk.Entry, tk.Listbox, tk.Button, tk.Checkbutton, tk.Radiobutton):
                control = cls(popup)
                self.assertIn(control.cget('background'), (theme.C['input_bg'], theme.C['surface2']))
                self.assertEqual(control.cget('foreground'), theme.C['body'])
            for name in ('TCombobox', 'TButton', 'TNotebook.Tab'):
                states = ('readonly',) if name == 'TCombobox' else ('selected',) if 'Tab' in name else ()
                foreground = style.lookup(name, 'foreground', states)
                background = style.lookup(name, 'fieldbackground' if name == 'TCombobox' else 'background', states)
                self.assertGreaterEqual(contrast(foreground, background), 4.5, name)
            self.assertGreaterEqual(contrast(theme.C['agent_accent'], theme.C['chat']), 4.5)
            self.assertGreaterEqual(contrast(theme.C['accent2'], theme.C['code_bg']), 4.5)
            for name in ('TNotebook.Tab', 'Coding.TNotebook.Tab', 'Explorer.TNotebook.Tab'):
                for state in ((), ('selected',)):
                    colour = style.lookup(name, 'lightcolor', state)
                    self.assertLess(max(int(colour[i:i+2], 16) for i in (1, 3, 5)), 195,
                                    (name, state, colour))
        finally:
            root.destroy()


class CodingAppearanceTests(unittest.TestCase):
    setUp = test_app.WorkbenchTests.setUp
    cleanup_tk = test_app.WorkbenchTests.cleanup_tk
    step = test_app.WorkbenchTests.step
    pump = test_app.WorkbenchTests.pump

    def test_surface_styles_keep_coding_density_local(self):
        style = ttk.Style(self.root)
        self.assertEqual(self.app.explorer.tree.cget('style'), 'Coding.Treeview')
        self.assertLess(int(style.lookup('Coding.Treeview', 'rowheight')), int(style.lookup('Treeview', 'rowheight')))
        self.assertEqual(self.app.agent_panel.notebook.cget('style'), 'Coding.TNotebook')
        self.assertEqual(self.app.bottom.notebook.cget('style'), 'Coding.TNotebook')
        self.assertEqual(self.app.chat.cget('background'), theme.C['chat'])

    def test_result_ink_uses_actual_lines_without_inventing_test_counts(self):
        output = self.app.output_views['测试']
        self.app.write_output('测试', 'test_read (Suite.test_read) ... ok\nOK\n'
                              'test_error (Suite.test_error) ... FAIL\nFAILED (failures=1)\n', clear=True)
        self.assertTrue(output.tag_ranges('forge_pass'))
        self.assertTrue(output.tag_ranges('forge_fail'))
        self.assertEqual(self.app.task_evidence.tests, [])
        self.assertIn('FAILED (failures=1)', output.get('1.0', 'end'))

    def test_identity_colours_keep_content_permission_and_file_links(self):
        chat = self.app.chat
        before = self.app.permission.get()
        chat.message('user', 'Inspect one.py')
        chat.message('Coding Agent', '## Result\n[one.py](one.py) was read.')
        self.assertTrue(chat.tag_ranges('user_heading'))
        self.assertTrue(chat.tag_ranges('agent_heading'))
        self.assertIn('was read.', chat.get('1.0', 'end'))
        self.assertEqual(self.app.permission.get(), before)
        self.assertTrue(any(name.startswith('file') for name in chat.tag_names()))

    def test_slim_scrollbar_keeps_real_text_scrolling(self):
        text = self.app.output_views['测试']
        self.app.write_output('测试', '\n'.join(str(i) for i in range(150)), clear=True)
        text.yview_moveto(0)
        first = text.yview()
        text.yview_scroll(20, 'units')
        self.assertGreater(text.yview()[0], first[0])


class BoundaryTests(unittest.TestCase):
    def test_visual_work_does_not_change_execution_or_pane_state_methods(self):
        root = Path(__file__).resolve().parents[1]
        path = 'forge-gui/development_mode/app.py'
        old = subprocess.check_output(['git', 'show', 'ddace89:' + path], cwd=root).decode('utf-8')
        # This boundary belongs to the visual-only commit. Later authorized
        # execution fixes must not be compared to the pre-visual send method.
        # Current pane invariants remain covered by OwnershipTests/layout tests.
        new = subprocess.check_output(['git', 'show', '120deae:' + path], cwd=root).decode('utf-8')
        def methods(source):
            lines = source.splitlines()
            cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
            return {n.name: '\n'.join(lines[n.lineno-1:n.end_lineno]) for n in cls.body if isinstance(n, ast.FunctionDef)}
        before, after = methods(old), methods(new)
        for name in ('apply_layout', 'place_sashes', '_panes_resized', 'capture_layout',
                     'toggle_ide', 'send', 'handle_event', 'permission_changed'):
            self.assertEqual(before[name], after[name], name)


if __name__ == '__main__':
    unittest.main()
