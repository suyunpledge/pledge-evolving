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
        # Hidden Windows roots can keep supplying window events; service the
        # real timer/idle callbacks explicitly so queued evidence can render.
        self.root.tk.dooneevent(_tkinter.TIMER_EVENTS | _tkinter.DONT_WAIT)
        self.root.tk.dooneevent(_tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT)
        for _ in range(16):
            if not self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                break

    def pump(self, condition, timeout=60):
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
            self.pump(lambda: self.app.input.winfo_ismapped())
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
        # v0.3：「取消」按钮已搬到 RuntimeContext.任务控制——通过
        # _runtime_ctx.get_task_controls() 找到对应按钮并触发。
        with patch.object(self.app.runtime, 'cancel', return_value=True) as cancel:
            task = self.app._runtime_ctx.get_task_controls()
            from tkinter import ttk as _ttk
            cancel_button = next(
                (w for w in _iter_all_widgets(task) if isinstance(w, _ttk.Button)
                 and w.cget('text') == '取消'),
                None)
            self.assertIsNotNone(cancel_button, 'RuntimeContext 应含「取消」按钮')
            cancel_button.invoke()
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

    # ── W5 批次（17 号审查 B1/B2/B4/E1）────────────────────────────

    def _tab_texts(self):
        # ttk.tabs() 会列出隐藏页；隐藏以 tab state='hidden' 判定
        return [self.app.log_tabs.tab(tab, 'text')
                for tab in self.app.log_tabs.tabs()
                if str(self.app.log_tabs.tab(tab, 'state')) != 'hidden']


def _iter_all_widgets(root):
    """深度优先遍历 root 及其所有 Tk 子控件（生成器）。"""
    stack = [root]
    while stack:
        widget = stack.pop()
        yield widget
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass

    def test_agent_events_bottom_tab_toggles_without_data_loss(self):
        # v0.2：事件视图统一在 AgentPanel「完整事件」Tab；底部 Notebook
        # 不再承载（_toggle_events_tab 改控 AgentPanel notebook）。
        self.assertEqual(self._tab_texts(), ['对话', '任务', '子 Agent', '完整事件'])
        # app.events 是 Text widget（嵌在 AgentEvents 里），其
        # .master.master 才是 完整事件 Tab 的 Frame（_events_holder）。
        self.assertIs(self.app.events.master.master, self.app._events_holder)
        # 写入内容应保留在事件流中（开关不影响已写入的日志）。
        self.app.append(self.app.events, 'w5-event-line\n')
        # dev_events=False → 隐藏 AgentPanel 第 4 个 Tab；True → 显示。
        self.app.dev_events.set(False)
        self.app._toggle_events_tab()
        self.step()
        self.app.dev_events.set(True)
        self.app._toggle_events_tab()
        self.step()
        self.assertIn('w5-event-line', self.app.events.get('1.0', 'end'))
        # v0.2：4 Tab（隐藏只是临时，Tab 集合不变）。
        self.assertEqual(self._tab_texts(), ['对话', '任务', '子 Agent', '完整事件'])

    def test_enter_sends_and_shift_enter_inserts_newline(self):
        """E1：Enter=发送、Shift+Enter=换行；IME 组合期不触发。"""

        class _Event:
            def __init__(self, state=0):
                self.state = state

        sent = []
        self.app.send = lambda **kw: sent.append(kw)
        self.app.input.insert('1.0', 'hello')
        self.assertEqual(self.app._enter_key(_Event(state=0)), 'break')
        self.assertEqual(sent, [{}])
        self.assertEqual(self.app._enter_key(_Event(state=0x0001)), None)
        self.assertEqual(len(sent), 1, 'Shift+Enter 不得触发发送')
        # IME 组合守卫（行内 IME 开启路径）
        self.app._inline_ime = type('Ime', (), {'composing': True})()
        self.assertEqual(self.app._enter_key(_Event(state=0)), None)
        self.assertEqual(len(sent), 1, '组合期 Enter 交给输入法')

    def test_perm_badge_lives_in_agent_column(self):
        """B1 修正回归：权限徽标挂 Agent 栏状态行，不再漂到工作台顶层。"""
        badge = self.app.perm_badge
        frame = badge.master
        self.assertIs(frame.master, self.app.agent_frame)
        self.assertIs(self.app.task_label.master, frame)

    def test_agent_groups_separate_session_task_and_model_rows(self):
        """B1：三组语义分区的代表控件都在 Agent 栏内且映射可见。

        v0.3：老「继承配置」区块（model_choice / cancel_button /
        delegate_button / run_model）已被 RuntimeContext 取代——
        模型下拉搬到 RuntimeContext.模型路由，操作按钮搬到
        RuntimeContext.任务控制。本测试只验仍由 agent_frame 直接
        承载的核心控件。
        """
        from tkinter import ttk as _ttk
        for widget in (self.app.task_label, self.app.perm_badge, self.app.context_label,
                       self.app.input, self.app.send_button):
            self.pump(lambda: widget.winfo_ismapped())
            self.assertTrue(widget.winfo_ismapped(), '分组后控件应可见')
        # RuntimeContext 是 v0.3 接管任务控制 / 模型 / 权限 的总入口——
        # 在这里验证它确实被挂载到了 AgentPanel。
        self.assertIsNotNone(self.app._runtime_ctx,
                             'RuntimeContext 应在 _build_agent 阶段挂载')
        # 任务组与模型组的下拉变量随行布局存在
        for var in (self.app.planning, self.app.permission, self.app.task_role):
            self.assertEqual(var.get() in {'low', 'read-only', 'default'}, True)
        # v0.3：任务角色 / 独立模型 / 模型回显已迁到 RuntimeContext——
        # agent_frame 不再承载这一行。验证 RuntimeContext 的任务控件
        # 包含「创建子 Agent」「重试」「复审」「恢复」按钮。
        task = self.app._runtime_ctx.get_task_controls()
        from tkinter import ttk as _ttk
        # TaskControls 内部用两行（row1 / row2）放控件，递归收集所有 button。
        button_texts = []
        for descendant in task.winfo_children():
            button_texts.extend(w.cget('text') for w in descendant.winfo_children()
                                 if isinstance(w, _ttk.Button))
            button_texts.extend(w.cget('text') for w in task.winfo_children()
                                 if isinstance(w, _ttk.Button))
        for label in ('创建子 Agent', '重试', '复审', '恢复', '执行测试', '取消'):
            self.assertIn(label, button_texts, f'RuntimeContext.任务控制缺 {label}')

    def test_focus_toggle_contract_survives_button_removal(self):
        """B4：按钮移除后 toggle_focus 状态机与预设联动保持原契约。"""
        self.state.set_layout(focus='editor')
        self.app.preset.set('focus')
        self.app.change_preset()
        self.step()
        self.assertEqual(len(self.app.panes.panes()), 1)
        self.app.toggle_focus()
        self.pump(lambda: self.app.input.winfo_ismapped())
        self.assertTrue(self.app.input.winfo_ismapped())
        self.assertEqual(self.app.preset.get(), 'focus')


if __name__ == '__main__':
    unittest.main()
