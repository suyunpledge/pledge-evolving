"""Real Tk geometry/interaction smoke, no network or shared configuration."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import _tkinter
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

    def step(self):
        # update() waits until *all* OS/Tcl events drain, which may never
        # happen while another app/test produces Windows events. Bound each
        # iteration so the timeout and assertions actually get evaluated.
        for _ in range(16):
            if not self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                break

    def pump(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.step()
            if condition():
                return
            time.sleep(.015)
        self.fail('Tk condition timed out: ' + self.app.status.get())

    def test_mode_roundtrip_keeps_buffers_runtime_and_parent(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        row['widget'].insert('end', '# unsaved\n')
        self.step()
        parent, agent = row['frame'].master, self.app.runtime.bridge.agent
        self.state.switch('general')
        self.step()
        self.assertEqual(len(self.app.panes.panes()), 1)
        self.state.switch('development')
        self.step()
        self.assertEqual(len(self.app.panes.panes()), 3)
        self.assertIs(self.app.current_row()['frame'].master, parent)
        self.assertIs(self.app.runtime.bridge.agent, agent)
        self.assertIn('unsaved', self.app.current_row()['widget'].get('1.0', 'end'))
        self.app.toggle_ide()
        self.assertEqual(self.state.mode.value, 'development')
        self.assertEqual(len(self.app.panes.panes()), 1)

    def test_clean_agent_write_refreshes_editor_from_actual_runtime(self):
        import json
        from forge.test_task_planning import Router
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        self.app.runtime.bridge.router = Router([
            json.dumps({'tool': 'read_file', 'args': {'path': 'one.py'}}),
            json.dumps({'tool': 'write_file', 'args': {'path': 'one.py', 'content': 'print(2)\n'}}),
            'Done'])
        agent = self.app.runtime.bridge.agent
        agent.thinking = False
        agent.thinking_mode = 'off'
        self.app.permission.set('workspace-write')
        self.app.planning.set('none')
        self.app.input.insert('1.0', 'change one.py')
        self.app.send()
        self.pump(lambda: 'print(2)' in self.app.current_row()['widget'].get('1.0', 'end'))
        self.pump(lambda: self.app._jobs == 0)
        self.assertFalse(self.app.current_row()['document'].dirty)
        self.assertTrue(self.app.task_evidence.actions)
        self.assertEqual(self.app.task_evidence.phase, 'completed')

    def test_reconciliation_preserves_new_typing_and_closed_tabs(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        path = str(row['document'].path)
        (self.project / 'one.py').write_text('print(2)\n')
        observation = row['document'].inspect_external()
        row['widget'].insert('end', '# just typed')
        self.app._reconcile_external([(path, row, row['document'].digest, observation)], None)
        self.assertIn('just typed', row['widget'].get('1.0', 'end'))
        self.assertEqual(row['document'].status, 'conflict')
        with patch('development_mode.app.messagebox.askyesno', return_value=True):
            self.app.close_current()
        self.app._reconcile_external([(path, row, row['document'].digest, observation)], None)
        self.assertFalse(self.app.documents)

    def test_attachment_is_latest_buffer_not_attachment_time_snapshot(self):
        calls = []
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        self.app.attach_current()
        self.app.current_row()['widget'].insert('end', '# latest draft')
        self.app.input.insert('1.0', 'inspect')
        with patch.object(self.app.runtime, 'run', side_effect=lambda p, **kw: (calls.append(kw) or {'text': 'ok'})):
            self.app.send()
            self.pump(lambda: self.app._jobs == 0)
        self.assertIn('latest draft', calls[0]['contexts'][0]['content'])

    def test_obsolete_disk_read_cannot_replace_newer_saved_baseline(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        document = row['document']
        baseline, observation = document.digest, document.inspect_external()
        row['widget'].insert('end', '# saved after read')
        self.app.document_changed(row)
        document.save()
        self.app._reconcile_external([(str(document.path), row, baseline, observation)], None)
        self.assertIn('saved after read', row['widget'].get('1.0', 'end'))
        self.assertEqual(document.text, (self.project / 'one.py').read_text())

    def test_checkpoint_confirmation_never_discards_dirty_buffer(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        row['widget'].insert('end', '# draft')
        self.step()
        preview = {'path': str(row['document'].path), 'commit': 'a' * 40, 'expected': 'hash', 'diff': 'x'}
        with patch.object(self.app.runtime, 'restore_checkpoint') as restore:
            with patch('development_mode.app.messagebox.askyesno', return_value=True) as ask:
                self.app.restore_reviewed_checkpoint(preview)
        restore.assert_not_called()
        ask.assert_not_called()
        self.assertIn('draft', row['widget'].get('1.0', 'end'))

    def test_integrated_preferences_inside_project_use_external_execution_home(self):
        self.pump(lambda: self.app._jobs == 0)
        self.app.destroy()
        self.state = ModeState(self.project / '.forge-development-home')
        self.state.switch('development')
        self.app = DevelopmentWorkbench(self.root, self.state)
        self.app.pack(fill='both', expand=True)
        self.app.open_project(self.project)
        self.pump(lambda: self.app.source is not None)
        self.assertFalse(self.app.runtime.home.is_relative_to(self.project.resolve()))
        self.assertTrue(self.state.path.is_relative_to(self.project.resolve()))
        self.assertTrue(self.app.runtime.bridge.agent.session.path.is_relative_to(self.app.runtime.home))

    def test_reopen_project_restores_real_history_without_running_pending_action(self):
        from forge.session import Session
        self.pump(lambda: self.app._jobs == 0)
        old = self.state.home / 'sessions' / 'interrupted.jsonl'
        with Session(old, meta={'cwd': str(self.project.resolve())}) as journal:
            journal.append('user_message', content='review my project')
            journal.append('workflow_state', run_id='interrupted', phase='executing')
            journal.append('tool_started', run_id='interrupted', action_id='unknown', tool='write_file')
        before = old.read_bytes()
        self.state.remember_session(old)
        self.app.destroy()
        self.app = DevelopmentWorkbench(self.root, self.state)
        self.app.pack(fill='both', expand=True)
        self.app.open_project(self.project)
        self.pump(lambda: 'review my project' in self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.task_evidence.actions['unknown']['status'], 'unknown')
        self.assertEqual(self.app.task_evidence.phase, 'interrupted')
        self.assertFalse(self.app.runtime.busy)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual((self.project / 'one.py').read_text(), 'print(1)\n')
        self.assertIsNone(self.app.runtime.bridge.last_result)

    def test_save_and_external_change_preserves_buffer(self):
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        row = self.app.current_row()
        row['widget'].insert('end', '# edit\n')
        self.step()
        self.app.save_current()
        self.pump(lambda: not row['saving'])
        self.assertIn('# edit', (self.project / 'one.py').read_text())
        row['widget'].insert('end', '# user draft\n')
        self.step()
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
                self.step()
                self.assertGreater(self.app.agent_frame.winfo_width(), 0)
        self.app.preset.set('editor')
        self.app.change_preset()
        self.step()
        self.app.open_file(self.project / 'one.py')
        self.pump(lambda: bool(self.app.documents))
        self.app.current_row()['widget'].focus_set()
        self.step()
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
            self.step()
            self.assertEqual(len(self.app.panes.panes()), 1)
            self.app.toggle_focus()
            self.step()
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

    def test_mode_switch_resets_next_task_permission(self):
        self.app.permission.set('workspace-write')
        self.app._permission = 'workspace-write'
        self.state.switch('general')
        self.assertEqual(self.app.permission.get(), 'read-only')
        self.state.switch('development')
        self.assertEqual(self.app.permission.get(), 'read-only')

    def test_test_execution_requires_exact_human_confirmation(self):
        from forge.test_task_planning import Router
        from unittest.mock import Mock
        (self.project / 'tests').mkdir()
        self.app.runtime.bridge.router = Router(['done'])
        agent = self.app.runtime.bridge.agent
        agent.thinking_mode, agent.thinking = 'off', False
        self.app.runtime.run('prepare tests', permission='workspace-write', planning='none')
        self.app.permission.set('workspace-write')
        self.pump(lambda: self.app._jobs == 0)
        execute = Mock(return_value={'status': 'completed'})
        with patch('development_mode.app.simpledialog.askstring', return_value='tests'), \
                patch('development_mode.app.messagebox.askyesno', return_value=False), \
                patch.object(self.app.runtime, 'execute_tests', execute):
            self.app.run_tests()
            execute.assert_not_called()
        with patch('development_mode.app.simpledialog.askstring', return_value='tests'), \
                patch('development_mode.app.messagebox.askyesno', return_value=True), \
                patch.object(self.app.runtime, 'execute_tests', execute):
            self.app.run_tests()
            self.pump(lambda: self.app._jobs == 0)
        execute.assert_called_once()
        self.assertTrue(execute.call_args.kwargs['trusted'])

    def test_cancel_button_requests_stop_without_marking_worker_complete(self):
        with patch.object(self.app.runtime, 'cancel', return_value=True) as cancel:
            self.app.cancel_button.invoke()
            cancel.assert_called_once()
        self.assertIn('等待', self.app.status.get())

    def test_recovery_preview_preserves_public_identity_and_resets_grant(self):
        from forge.session import Session
        from forge.secrets import redact
        identifier = 'rollout-display-fixture-123'
        journal = self.app.runtime.home / 'sessions' / (identifier + '.jsonl')
        with Session(journal, meta={'cwd': str(self.project.resolve())}) as log:
            log.append('user_message', content='actual dialogue')
        self.app.permission.set('workspace-write')
        self.app.restore_session(journal)
        self.pump(lambda: self.app._jobs == 0)
        self.assertIn(identifier, self.app.events.get('1.0', 'end'))
        self.assertEqual(redact(identifier), identifier)
        self.assertEqual(self.app.permission.get(), 'read-only')


if __name__ == '__main__':
    unittest.main()
