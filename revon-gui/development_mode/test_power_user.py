"""极客层：真实按键触发快速打开 / 命令面板，命令面板执行与不可用说明。"""
import unittest
from unittest.mock import patch

from development_mode import shortcuts
from development_mode.command_palette import CommandPalette
from development_mode.quick_open import QuickOpen
from development_mode.test_layout_regression import _WorkbenchFixture


class _Keys(_WorkbenchFixture):
    def press(self, sequence):
        root = self.app.winfo_toplevel()
        root.focus_force()
        self.root.update()
        root.event_generate(sequence)
        self.root.update()


class ShortcutKeyTests(_Keys, unittest.TestCase):
    def test_plain_ctrl_p_opens_quick_open_and_ctrl_shift_p_opens_palette(self):
        # 回归：键位表曾把 Mod+P 转成 <Control-P>，单按 Ctrl+P 毫无反应。
        self.press('<Control-p>')
        self.assertIsInstance(self.app._quick, QuickOpen)
        self.assertNotIsInstance(self.app._quick, CommandPalette)
        self.app._quick.close()
        self.press('<Control-Shift-P>')
        self.assertIsInstance(self.app._palette, CommandPalette)
        self.assertIsNone(self.app._quick)
        self.app._palette.close()

    def test_every_global_sequence_matches_what_tk_generates(self):
        for item in shortcuts.SHORTCUTS:
            letters = [t for t in item.keys.split('+') if len(t) == 1 and t.isalpha()]
            if not letters or item.scope != 'global':
                continue
            expect_upper = 'Shift' in item.keys
            # 序列末尾是 '<…-x>' 的字母：有 Shift 才大写。
            self.assertEqual(item.sequence[-2].isupper(), expect_upper, item.key)


class PaletteTests(_Keys, unittest.TestCase):
    def test_palette_runs_command_and_explains_unavailable_ones(self):
        palette = self.app.command_palette()
        self.addCleanup(lambda: palette.alive and palette.close())
        labels = palette._paths
        self.assertIn('快速打开文件…', labels)
        self.assertIn('权限：可改项目文件（需确认）', labels)
        # 没有打开文件：保存不可用，执行只给出原因
        palette.query.set('保存当前文档')
        palette._accept()
        self.assertIsNone(palette.ran)
        self.assertIn('没有打开的文件', self.app.status.get())

        palette = self.app.command_palette()
        with patch('development_mode.app.messagebox.askyesno', return_value=True):
            palette.query.set('可改项目文件')
            palette._accept()
        self.assertEqual(palette.ran, 'perm_write')
        self.assertEqual(self.app.permission.get(), 'workspace-write')

    def test_palette_lists_shortcuts_next_to_commands(self):
        commands = {c.key: c for c in self.app._palette_commands()}
        self.assertEqual(commands['quick_open'].shortcut, shortcuts.hint('quick_open'))
        self.assertEqual(commands['run_tests'].shortcut, 'F5')
        self.assertEqual(commands['cancel_task'].unavailable, '当前没有运行中的任务')

    def test_welcome_hints_only_advertise_bound_shortcuts(self):
        bound = {item.display for item in shortcuts.SHORTCUTS if item.scope == 'global'}
        for chip_text in self.app.welcome._get_shortcut_chips():
            self.assertIn(chip_text.split()[0], bound, chip_text)



class TimelineTests(_Keys, unittest.TestCase):
    def emit(self, **data):
        self.app.handle_event({'event': 'progress', 'data': data})
        self.pump(lambda: not getattr(self.app, '_agent_render_pending', False))

    def timeline_text(self):
        summary = self.app.agent_panel._summary
        texts = []
        for line in summary.timeline.winfo_children():
            texts.append(' '.join(str(w.cget('text')) for w in line.winfo_children()
                                  if w.winfo_class() == 'Label') if line.winfo_class() == 'Frame'
                         else str(line.cget('text')))
        return ' | '.join(texts)

    def test_steps_show_tool_subject_status_timing_and_real_tokens(self):
        self.emit(type='development_task_started', task_id='t', phase='executing')
        self.emit(type='workflow_state', task_id='t', run_id='r', phase='running')
        self.emit(type='tool_started', task_id='t', run_id='r', action_id='a', tool='read_file')
        text = self.timeline_text()
        self.assertIn('read_file', text)
        self.assertIn('进行中', text)
        self.assertIsNone(self.app.chat._empty_card, '有卡片后空态覆盖层必须撤下')
        self.emit(type='tool_completed', task_id='t', run_id='r', action_id='a', tool='read_file',
                  ok=True, args={'path': 'src/app/main.py'}, result='ok')
        self.emit(type='tool_started', task_id='t', run_id='r', action_id='b', tool='shell_exec')
        self.emit(type='tool_completed', task_id='t', run_id='r', action_id='b', tool='shell_exec',
                  ok=False, args={'command': 'pytest -q'}, result='1 failed')
        self.emit(type='usage', task_id='t', run_id='r', model='m', total_tokens=18400)
        text = self.timeline_text()
        self.assertIn('main.py', text)  # 窄面板会从左截断，文件名必须保留
        self.assertIn('pytest -q', text)
        self.assertNotIn('进行中', text)
        header = self.app.agent_panel._summary.summary.cget('text')
        self.assertIn('2 步', header)
        self.assertIn('18.4k tokens', header)
        # 模型 'm' 不在价格表里：如实说价格未知，不显示 ¥0
        self.assertIn('价格未知', header)
        self.assertNotIn('¥', header)

    def test_cost_uses_invoice_rates_and_flags_unknown_models(self):
        from development_mode.agent_panel import estimate_cost, format_cost
        known = [{'model': 'deepseek-flash', 'tokens': 1_000_000}]
        self.assertEqual(format_cost(*estimate_cost(known)), '≈¥0.16')
        mixed = known + [{'model': 'some-new-model', 'tokens': 500}]
        self.assertEqual(format_cost(*estimate_cost(mixed)), '≈¥0.16 · 部分价格未知')
        self.assertEqual(format_cost(*estimate_cost([{'model': 'x', 'tokens': 0}])), '')

    def test_long_runs_keep_only_recent_steps_visible(self):
        self.emit(type='development_task_started', task_id='t', phase='executing')
        for index in range(12):
            self.emit(type='tool_completed', task_id='t', action_id=f'a{index}', tool='read_file',
                      ok=True, args={'path': f'f{index}.py'})
        text = self.timeline_text()
        self.assertIn('更早的 4 步', text)
        self.assertIn('f11.py', text)
        self.assertNotIn('f0.py', text)


    def test_changes_summary_totals_files_and_opens_diff_per_file(self):
        self.emit(type='development_task_started', task_id='t', phase='executing')
        for action, path, added, removed in (('w1', 'src/a.py', 10, 2), ('w2', 'src/b.py', 3, 0),
                                             ('w3', 'src/a.py', 1, 1)):
            self.emit(type='tool_completed', task_id='t', action_id=action, tool='edit_file', ok=True,
                      args={'path': path}, meta={'file_changes': [
                          {'path': path, 'state': 'written', 'added': added, 'removed': removed}]})
        summary = self.app.agent_panel._changes_summary
        self.assertEqual(summary.title.cget('text'), '改动 2 个文件')
        self.assertEqual(summary.totals.cget('text'), '+14 −3')
        with patch.object(summary, '_view') as view:
            summary.view_file(0)
        view.assert_called_once()
        self.assertEqual(view.call_args.args[0]['path'], 'src/a.py')



class DensityTests(_Keys, unittest.TestCase):
    def bottom_mounted(self):
        return self.app.bottom in [self.app.vertical.nametowidget(n) for n in self.app.vertical.panes()]

    def test_simple_hides_bottom_and_runtime_context_and_is_remembered(self):
        panel = self.app.agent_panel
        self.pump(lambda: self.bottom_mounted())
        self.app.set_density('simple')
        self.root.update()
        self.assertFalse(self.bottom_mounted())
        self.assertEqual(panel._density_chip.cget('text'), '简洁')
        if panel._runtime_context is not None:
            self.assertFalse(panel._runtime_context.winfo_manager())
        # apply_layout 补回底部后，挂点会再次收起
        self.app.apply_layout()
        self.root.update()
        self.assertFalse(self.bottom_mounted())
        self.assertIn('simple', (self.state.home / 'coding_view.json').read_text(encoding='utf-8'))
        self.app.toggle_density()
        self.root.update()
        self.assertTrue(self.bottom_mounted())
        self.assertEqual(panel._density_chip.cget('text'), '专业')


if __name__ == '__main__':
    unittest.main()
