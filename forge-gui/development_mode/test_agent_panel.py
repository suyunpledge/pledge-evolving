"""Actual Windows Tk widgets and Runtime, hidden windows; no visual QA claim."""
import ast
import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import patch

from forge.development.evidence import TaskEvidence
from forge.test_task_planning import Router
from . import test_app


class AgentPanelTests(unittest.TestCase):
    setUp = test_app.WorkbenchTests.setUp
    cleanup_tk = test_app.WorkbenchTests.cleanup_tk
    step = test_app.WorkbenchTests.step
    pump = test_app.WorkbenchTests.pump

    def emit(self, **data):
        self.app.handle_event({'event': 'progress', 'data': data})
        self.pump(lambda: not getattr(self.app, '_agent_render_pending', False))

    def task(self):
        self.emit(type='development_task_started', task_id='t', phase='executing')

    def test_three_tabs_events_mount_once_and_no_parallel_event_flow(self):
        panel = self.app.agent_panel
        self.assertEqual([panel.notebook.tab(t, 'text') for t in panel.notebook.tabs()],
                         ['Conversation', 'Tasks', 'Agents'])
        first = panel.mount_events(self.app.output)
        self.assertIs(first, panel.mount_events(self.app.output))
        self.task()
        self.assertIn('development_task_started', self.app.events.get('1.0', 'end'))
        self.assertEqual(self.app.events.master.master, self.app.output)

    def test_markdown_code_and_instance_file_links_keep_work_handler(self):
        import chat_widgets
        marker = lambda path: False
        with patch.object(chat_widgets, '_FILE_LINK_HANDLER', marker):
            chat = self.app.chat
            chat.message('Coding Agent', '# Finding\n\n**bold** and `inline`\n\n[open](one.py)\n\n```python\nprint(1)\n```')
            self.assertTrue(chat.tag_ranges('b'))
            self.assertTrue(chat.tag_ranges('code'))
            self.assertTrue(chat.tag_ranges('heading'))
            self.assertTrue(chat.tag_ranges('file1'))
            self.assertIs(chat_widgets._FILE_LINK_HANDLER, marker)
            with patch.object(self.app, 'open_file') as opened:
                chat.open_file('one.py:1')
                opened.assert_called_once_with((self.project / 'one.py').resolve())
                chat.open_file('../outside.py')
                self.assertEqual(opened.call_count, 1)

    def test_tool_summary_collapses_and_updates_existing_status(self):
        self.task()
        self.emit(type='tool_started', task_id='t', action_id='a', tool='read_file')
        summary = self.app.agent_panel._summary
        self.assertFalse(summary.expanded)
        self.assertIn('running', summary.summary.cget('text'))
        self.emit(type='tool_completed', task_id='t', action_id='a', tool='read_file', ok=False, result='denied')
        self.assertIn('failed', self.app.workflow.get('1.0', 'end'))
        self.assertNotIn('running', self.app.workflow.get('1.0', 'end'))
        summary.button.invoke()
        self.assertTrue(summary.expanded)
        self.assertIn('denied', summary.detail.get('1.0', 'end'))
        self.assertNotIn('denied', self.app.chat.get('1.0', 'end'), 'tool details stay folded by default')

    def test_plan_never_has_fake_completed_stages(self):
        self.task()
        self.emit(type='task_plan', task_id='t', plan={'tasks': ['inspect', 'test']})
        self.emit(type='development_task_finished', task_id='t', phase='completed')
        text = self.app.workflow.get('1.0', 'end')
        self.assertIn('○ inspect', text)
        self.assertIn('○ test', text)
        self.assertNotIn('✓ test', text)

    def test_agent_tree_uses_parent_run_and_distinct_runs(self):
        self.task()
        for name, run, parent, depth in [('forge', 'parent', '', 0), ('forge/sub1', 'child', 'parent', 1), ('forge/sub1', 'other', 'parent', 1)]:
            self.emit(type='development_agent_config', task_id='t', agent=name, selected_model=['p', 'm'],
                      model_source='independent', permission='read-only', tool_surface=['read_file'])
            self.emit(type='workflow_state', task_id='t', agent=name, run_id=run, parent_run_id=parent, depth=depth, phase='executing')
        panel = self.app.agent_panel
        parent = next(i for i, r in panel.rows.items() if r['run_id'] == 'parent')
        children = panel.tree.get_children(parent)
        self.assertEqual(len(children), 2)
        panel.tree.selection_set(children[0]); panel.show_agent()
        text = panel.detail.get('1.0', 'end')
        self.assertIn('independent', text)
        self.assertIn('read_file', text)
        self.assertIn('read-only', text)
        self.emit(type='development_task_started', task_id='next', phase='executing')
        self.assertFalse(panel.rows)
        self.assertFalse(panel.tree.get_children())

    def test_review_agent_shows_real_result_and_parent(self):
        self.task()
        self.emit(type='workflow_state', task_id='t', agent='forge', run_id='parent', phase='completed')
        self.emit(type='review_result', task_id='t', run_id='parent', message_id='parent',
                  review_id='review', review={'model': 'actual', 'text': 'real finding'})
        panel = self.app.agent_panel
        identifier = next(i for i, r in panel.rows.items() if r['agent'] == 'Review Agent')
        panel.tree.selection_set(identifier); panel.show_agent()
        self.assertIn('real finding', panel.detail.get('1.0', 'end'))
        self.assertIn('actual', panel.detail.get('1.0', 'end'))
        self.assertNotEqual(panel.tree.parent(identifier), '')

    def test_diff_states_have_different_operations(self):
        self.task()
        self.emit(type='tool_completed', task_id='t', action_id='pending', tool='write_file',
                  ok=False, args={'path': 'one.py'}, meta={'requires_approval': True})
        self.emit(type='tool_completed', task_id='t', action_id='written', tool='write_file', ok=True,
                  meta={'checkpoint': 'a' * 40, 'file_changes': [{'path': 'one.py', 'state': 'written',
                    'revision': 'hash', 'before_revision': 'old', 'diff': '+changed', 'added': 1, 'removed': 0}]})
        cards = self.app.agent_panel._cards
        self.assertIn('pending', cards[('pending', 'one.py')].buttons)
        self.assertNotIn('revert', cards[('pending', 'one.py')].buttons)
        written = cards[('written', 'one.py')]
        self.assertIn('revert', written.buttons)
        written.fit(180)
        self.step()
        self.assertGreater(written.buttons['revert'].grid_info()['row'], 0)
        written.buttons['view'].invoke()
        self.assertIn('+changed', self.app.output_views['Diff'].get('1.0', 'end'))
        self.emit(type='development_change_state', task_id='t', action_id='written', path='one.py', state='committed', commit='b' * 40)
        self.assertNotIn('revert', written.buttons)
        self.assertIn('已提交', written.state_label.cget('text'))

    def run_actual_edit(self):
        rt = self.app.runtime
        rt.bridge.router = Router([json.dumps({'tool': 'read_file', 'args': {'path': 'one.py'}}),
            json.dumps({'tool': 'write_file', 'args': {'path': 'one.py', 'content': 'print(2)\n'}}), '# Done\nChanged one.py'])
        rt.bridge.agent.thinking = False
        rt.bridge.agent.thinking_mode = 'off'
        self.app.permission.set('workspace-write')
        self.app.planning.set('none')
        self.app.input.insert('1.0', 'change one.py')
        self.app.send()
        self.pump(lambda: self.app._jobs == 0 and bool(self.app.agent_panel._cards))
        self.step()
        return next(iter(self.app.agent_panel._cards.values()))

    def test_actual_agent_edit_card_view_and_authorized_revert(self):
        card = self.run_actual_edit()
        self.assertIn('已写入', card.state_label.cget('text'))
        self.assertEqual((card.change['added'], card.change['removed']), (1, 1))
        card.buttons['view'].invoke()
        self.assertIn('+print(2)', self.app.output_views['Diff'].get('1.0', 'end'))
        with patch('development_mode.app.messagebox.askyesno', return_value=True):
            card.buttons['revert'].invoke()
            self.pump(lambda: self.app._jobs == 0 and (self.project / 'one.py').read_text() == 'print(1)\n')
        self.assertNotIn('revert', card.buttons)

    def test_revert_card_refuses_new_disk_revision(self):
        card = self.run_actual_edit()
        (self.project / 'one.py').write_text('new external version\n')
        with patch.object(self.app.runtime, 'restore_checkpoint') as restore:
            card.buttons['revert'].invoke()
            self.pump(lambda: self.app._jobs == 0)
            restore.assert_not_called()
        self.assertIn('拒绝', self.app.status.get())
        # Force the pending scan's completion after the refusal, regardless of
        # scheduler timing; it must still refresh the tree without hiding why.
        notice = self.app.status.get()
        marker = self.project / 'after-refusal.txt'
        marker.write_text('scan still runs\n')
        scan = self.app.source.scan(set(self.app.expanded), '')
        self.app._project_scan(scan, None)
        self.assertTrue(self.app.tree.exists(str(marker.resolve())))
        self.assertEqual(self.app.status.get(), notice)
        self.assertEqual((self.project / 'one.py').read_text(), 'new external version\n')

    def test_project_scan_preserves_notices_for_two_seconds_without_renewal(self):
        scan = self.app.source.scan(set(self.app.expanded), '')
        for notice in ('已保存 one.py', '磁盘已变化，拒绝恢复旧卡片版本', '读取文件失败'):
            with self.subTest(notice=notice):
                with patch('development_mode.app.time.monotonic', return_value=100.0):
                    self.app.status.set(notice)
                for now in (100.0, 100.8, 101.999):
                    with patch('development_mode.app.time.monotonic', return_value=now):
                        self.app._project_scan(scan, None)
                    self.assertEqual(self.app.status.get(), notice)
                with patch('development_mode.app.time.monotonic', return_value=102.0):
                    self.app._project_scan(scan, None)
                self.assertIn('个 Git 变更', self.app.status.get())
                # A second background refresh must update immediately; it did
                # not start a new semantic-notice window at the previous write.
                with patch('development_mode.app.time.monotonic', return_value=102.1):
                    self.app._project_scan({**scan, 'branch': 'next-branch'}, None)
                self.assertTrue(self.app.status.get().startswith('next-branch ·'))

    def test_unknown_action_is_not_retried(self):
        self.task()
        self.emit(type='tool_completed', task_id='t', action_id='a', tool='write_file', ok=False, execution_state='unknown')
        self.app._retry_request = {'prompt': 'change source', 'delegate': False}
        with patch.object(self.app.runtime, 'run') as run:
            self.app.retry_agent_task()
            run.assert_not_called()
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), '')

    def test_retry_sends_restored_original_draft_but_keeps_new_typing(self):
        self.app._retry_request = {'prompt': 'original', 'delegate': True}
        self.app.input.insert('1.0', 'original')
        with patch.object(self.app, 'send') as send:
            self.app.retry_agent_task()
            send.assert_called_once_with(delegate=True)
        self.app.input.delete('1.0', 'end')
        self.app.input.insert('1.0', 'new human draft')
        with patch.object(self.app, 'send') as send:
            self.app.retry_agent_task()
            send.assert_not_called()
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'new human draft')

    def test_clear_history_removes_cards_and_restoration_renders_markdown(self):
        self.task()
        self.emit(type='tool_started', task_id='t', action_id='a', tool='read_file')
        old = self.app.agent_panel._summary
        self.app.chat.clear()
        self.assertFalse(old.winfo_exists())
        self.app.append(self.app.chat, '\nassistant\n**restored**\n')
        self.assertTrue(self.app.chat.tag_ranges('b'))
        self.app.render_evidence()
        self.pump(lambda: not self.app._agent_render_pending)
        self.assertIsNot(old, self.app.agent_panel._summary)

    def test_background_task_survives_twenty_mode_and_ide_toggles(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingRouter(Router):
            def complete(self, *args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test wait expired')
                return super().complete(*args, **kwargs)
        self.app.runtime.bridge.router = BlockingRouter(['real completion'])
        agent = self.app.runtime.bridge.agent
        agent.thinking, agent.thinking_mode = False, 'off'
        self.app.planning.set('none')
        self.app.input.insert('1.0', 'background task')
        self.app.send()
        try:
            self.pump(entered.is_set)
            identity = self.app.runtime.bridge.agent
            for _ in range(20):
                self.app.toggle_ide(); self.step()
            for _ in range(10):
                self.state.switch('general'); self.step()
                self.state.switch('development'); self.step()
            self.assertTrue(self.app.runtime.busy)
            self.assertIs(identity, self.app.runtime.bridge.agent)
            self.assertFalse(self.app.runtime._cancel_event.is_set())
            self.assertEqual(len(self.app.panes.panes()), 3)
        finally:
            release.set()
        self.pump(lambda: self.app._jobs == 0)
        self.assertIn('real completion', self.app.chat.get('1.0', 'end'))

    def test_model_dialog_starts_with_selected_child_and_invalid_source(self):
        self.state.configure_agent_model('forge/sub2', ('missing', 'model'))
        self.app.agent_model_settings('forge/sub2')
        window = next(w for w in self.app.winfo_children() if w.winfo_class() == 'Toplevel')
        combos = [w for w in window.winfo_children() if w.winfo_class() == 'TCombobox']
        self.assertEqual(combos[0].get(), 'forge/sub2')
        labels = [w.cget('text') for w in window.winfo_children() if w.winfo_class() == 'TLabel']
        self.assertTrue(any('失效' in text for text in labels))
        window.destroy()

    def test_permission_badge_tracks_rejected_grant_and_mode_reset(self):
        self.app.permission.set('workspace-write')
        self.assertIn('workspace-write', self.app.perm_badge.cget('text'))
        with patch('development_mode.app.messagebox.askyesno', return_value=False):
            self.app.permission_changed()
        self.assertEqual(self.app.permission.get(), 'read-only')
        self.assertIn('read-only', self.app.perm_badge.cget('text'))
        self.app.permission.set('workspace-write')
        self.state.switch('general')
        self.assertIn('read-only', self.app.perm_badge.cget('text'))


class OwnershipTests(unittest.TestCase):
    def test_protected_layout_methods_are_byte_identical_to_51f6f82(self):
        root = Path(__file__).resolve().parents[2]
        path = 'forge-gui/development_mode/app.py'
        old = subprocess.run(['git', 'show', '51f6f82:' + path], cwd=root, capture_output=True, check=True).stdout.decode('utf-8')
        new = (root / path).read_text(encoding='utf-8')
        def methods(source):
            lines = source.splitlines()
            cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
            return {n.name: '\n'.join(lines[n.lineno - 1:n.end_lineno]) for n in cls.body if isinstance(n, ast.FunctionDef)}
        before, after = methods(old), methods(new)
        for name in ('_build', '_build_project', '_build_editor', 'apply_layout', 'place_sashes', '_panes_resized', 'capture_layout', 'toggle_ide'):
            self.assertEqual(before[name], after[name], name)


if __name__ == '__main__':
    unittest.main()
