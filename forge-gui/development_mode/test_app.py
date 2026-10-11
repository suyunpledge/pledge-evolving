"""Real Tk geometry/interaction smoke, no network or shared configuration."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from gui_theme import apply_ttk_theme
from forge.development.state import ModeState
from development_mode.app import DevelopmentWorkbench


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'repo'
        self.project.mkdir()
        (self.project / 'one.py').write_text('print(1)\n', encoding='utf-8')
        self.root = tk.Tk()
        self.root.geometry('1200x760')
        self.root.attributes('-alpha', 0)
        self.addCleanup(self.cleanup_tk)
        apply_ttk_theme(self.root)
        self.state = ModeState(self.base / 'home')
        self.state.switch('development')
        self.app = DevelopmentWorkbench(self.root, self.state)
        self.app.pack(fill='both', expand=True)
        self.app.open_project(self.project)
        self.pump(lambda: self.app.source is not None and bool(self.app.tree.get_children()))

    def cleanup_tk(self):
        import gc
        if self.app.winfo_exists():
            self.pump(lambda: self.app._jobs == 0)
        self.root.destroy()
        self.app = None
        self.root = None
        gc.collect()

    def pump(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if condition():
                return
            time.sleep(.015)
        self.fail('Tk condition timed out: ' + self.app.status.get())

    def test_mode_roundtrip_keeps_buffers_runtime_and_parent(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        row['widget'].insert('end', '# unsaved\n')
        self.root.update()
        parent, agent = row['frame'].master, self.app.runtime.bridge.agent
        self.state.switch('general')
        self.root.update()
        self.assertEqual(len(self.app.panes.panes()), 1)
        self.state.switch('development')
        self.root.update()
        self.assertEqual(len(self.app.panes.panes()), 3)
        self.assertIs(self.app.current_row()['frame'].master, parent)
        self.assertIs(self.app.runtime.bridge.agent, agent)
        self.assertIn('unsaved', self.app.current_row()['widget'].get('1.0', 'end'))
        self.app.toggle_ide()
        self.assertEqual(self.state.mode.value, 'development')
        self.assertEqual(len(self.app.panes.panes()), 1)

    def test_save_and_external_change_preserves_buffer(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        row['widget'].insert('end', '# edit\n')
        self.root.update()
        self.app.save_current()
        self.pump(lambda: not row['saving'])
        self.assertIn('# edit', (self.project / 'one.py').read_text())
        row['widget'].insert('end', '# user draft\n')
        self.root.update()
        (self.project / 'one.py').write_text('external\n')
        self.app._check_external()
        self.pump(lambda: row['document'].status == 'conflict')
        self.assertIn('user draft', row['widget'].get('1.0', 'end'))
        self.app.save_current()
        self.pump(lambda: not row['saving'])
        self.assertEqual((self.project / 'one.py').read_text(), 'external\n')

    def test_project_tree_editor_focus_and_layout_presets(self):
        for size in ('760x540', '1200x760', '1920x1080'):
            self.root.geometry(size)
            for preset in ('agent', 'editor', 'focus'):
                self.app.preset.set(preset)
                self.app.change_preset()
                self.root.update()
                self.assertGreater(self.app.agent_frame.winfo_width(), 0)
        self.app.preset.set('editor')
        self.app.change_preset()
        self.root.update()
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        self.app.current_row()['widget'].focus_set()
        self.root.update()
        self.app.current_row()['widget'].insert('end', 'abc')
        self.assertIn('abc', self.app.current_row()['widget'].get('1.0', 'end'))

    def test_destroy_cancels_only_own_callbacks(self):
        own = set(self.app._after_ids)
        unrelated = self.root.after(30000, lambda: None)
        self.app.destroy()
        active = set(self.root.tk.splitlist(self.root.tk.call('after', 'info')))
        self.assertFalse(active.intersection(own))
        self.assertIn(unrelated, active)
        self.assertEqual(self.state._listeners, [])
        self.root.after_cancel(unrelated)

    def test_project_switch_resets_grant_and_keeps_checkpoint_stores_separate(self):
        self.pump(lambda: self.app._jobs == 0)
        old_checkpoint = self.app.runtime.bridge.agent.checkpoints.git_dir
        other = self.base / 'other'
        other.mkdir()
        self.app.permission.set('workspace-write')
        self.app._permission = 'workspace-write'
        self.app.open_project(other)
        self.pump(lambda: not self.app._loading)
        self.assertEqual(self.app.permission.get(), 'read-only')
        self.assertNotEqual(self.app.runtime.bridge.agent.checkpoints.git_dir, old_checkpoint)
        self.assertEqual(self.app.source.root, other.resolve())

    def test_scrollbar_and_focus_presets_in_five_tk_scales(self):
        for scale in (1, 1.25, 1.5, 1.75, 2):
            self.root.tk.call('tk', 'scaling', scale * 96 / 72)
            self.state.set_layout(focus='editor')
            self.app.preset.set('focus')
            self.app.change_preset()
            self.root.update()
            self.assertEqual(len(self.app.panes.panes()), 1)
            self.app.toggle_focus()
            self.root.update()
            self.assertTrue(self.app.input.winfo_ismapped())
            self.assertGreater(self.app.input.winfo_height(), 30)

    def test_send_uses_actual_agent_events_and_retains_draft_on_error(self):
        from forge.test_vscode_bridge import FakeRouter
        fake = self.app.runtime.bridge.router = FakeRouter()
        self.app.runtime.bridge.agent.thinking_mode = 'off'
        self.app.runtime.bridge.agent.thinking = False
        self.app.planning.set('none')
        self.app.input.insert('1.0', 'real request')
        self.app.send()
        self.pump(lambda: bool(fake.messages) and str(self.app.send_button['state']) == 'normal')
        self.assertIn('Offline reply', self.app.chat.get('1.0', 'end'))
        self.assertIn('workflow_state', self.app.events.get('1.0', 'end'))
        with patch.object(self.app.runtime, 'run', side_effect=ValueError('offline failure')):
            self.app.input.insert('1.0', 'retry draft')
            self.app.send()
            self.pump(lambda: str(self.app.send_button['state']) == 'normal')
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'retry draft')


if __name__ == '__main__':
    unittest.main()
