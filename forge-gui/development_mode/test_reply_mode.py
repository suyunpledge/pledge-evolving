"""User-controlled coding replies through the actual Tk submission path, offline."""
import unittest
from unittest.mock import patch

from forge.development.state import ModeState
from forge.test_task_planning import Router, plan_json
from development_mode import test_submission


class ReplyModeTests(unittest.TestCase):
    setUp = test_submission.SubmissionTests.setUp
    cleanup_tk = test_submission.SubmissionTests.cleanup_tk
    step = test_submission.SubmissionTests.step
    pump = test_submission.SubmissionTests.pump
    blocked = test_submission.SubmissionTests.blocked
    offline = test_submission.SubmissionTests.offline

    def choose(self, level, widget=None):
        choice = widget or self.app.reply_choice
        choice.combo.current(list(choice.LABELS).index(level))
        choice.combo.event_generate('<<ComboboxSelected>>')
        self.step()

    def test_default_input_sends_one_normal_reply_without_plan_card(self):
        self.assertEqual(self.app.planning.get(), 'none')
        self.assertEqual(self.app.reply_choice.combo.get(), '直接回答')
        router = self.app.runtime.bridge.router = Router(['你好，有什么编程问题？'])
        self.app.input.insert('1.0', '你好')
        self.app.send_button.invoke()
        self.pump(lambda: bool(self.app.runtime.tasks) and self.app._jobs == 0)
        self.assertEqual(len(router.calls), 1)
        self.assertIn('你好，有什么编程问题？', self.app.chat.get('1.0', 'end'))
        self.assertNotIn('任务结果', self.app.chat.get('1.0', 'end'))
        self.assertNotIn('正在创建 Agent', self.app.chat.get('1.0', 'end'))
        self.assertIsNone(self.app.agent_panel._planning_card)

    def test_both_selectors_sync_and_user_choices_survive_reopen(self):
        task = self.app._runtime_ctx.get_task_controls()
        for level in ('low', 'medium', 'high', 'none'):
            self.choose(level)
            self.assertEqual(task.get_planning(), level)
            self.assertEqual(self.app.planning.get(), level)
            self.assertEqual(ModeState(self.state.home).value('planning'), level)
        self.choose('high', task._planning)
        self.assertEqual(self.app.reply_choice.get(), 'high')
        self.assertEqual(self.app.planning.get(), 'high')

    def test_failed_save_keeps_both_selectors_and_saved_preference(self):
        with patch.object(self.state, '_save', side_effect=OSError('disk unavailable')):
            self.choose('high')
        self.assertEqual(self.app.reply_choice.get(), 'none')
        self.assertEqual(self.app._runtime_ctx.get_task()['planning'], 'none')
        self.assertEqual(self.state.value('planning'), 'none')
        self.assertIn('未保存', self.app.status.get())

    def test_user_selected_plan_runs_before_answer(self):
        self.offline(Router([plan_json(), '已经规划后回答']))
        self.choose('medium')
        self.app.input.insert('1.0', '解释程序结构')
        self.app.send_button.invoke()
        self.pump(lambda: bool(self.app.runtime.tasks) and self.app._jobs == 0)
        self.assertEqual(len(self.app.runtime.bridge.router.calls), 2)
        self.assertTrue(self.app.task_evidence.plan)
        self.assertIsNotNone(self.app.agent_panel._planning_card)
        self.assertEqual(self.app.agent_panel.task_context['planning'], 'medium')
        self.assertIn('已经规划后回答', self.app.chat.get('1.0', 'end'))

    def test_queued_reply_keeps_the_choice_at_send_time(self):
        release = self.blocked()
        router = self.app.runtime.bridge.router
        router.answers = iter(['first finished', plan_json(), 'second finished'])
        self.choose('low')
        self.app.input.insert('1.0', 'queued with a plan')
        self.app.send_button.invoke()
        self.choose('none')
        release.set()
        self.pump(lambda: len(self.app.runtime.tasks) == 2 and self.app._jobs == 0)
        self.assertEqual(len(router.calls), 3)
        self.assertEqual(self.app.agent_panel.task_context['planning'], 'low')
        self.assertEqual(self.app.planning.get(), 'none')

    def test_selector_is_visible_with_settings_collapsed_at_compact_sizes(self):
        self.app.toggle_ide()
        for width, height in ((760, 540), (1200, 760)):
            self.root.geometry(f'{width}x{height}')
            self.pump(lambda: self.root.winfo_width() == width and
                      self.root.winfo_height() == height and
                      self.app.agent_frame.winfo_rooty() + self.app.agent_frame.winfo_height()
                      <= self.root.winfo_rooty() + height and
                      self.app.input_bar.winfo_y() + self.app.input_bar.winfo_height()
                      <= self.app.agent_frame.winfo_height())
            self.step()
            combo = self.app.reply_choice.combo
            self.assertTrue(combo.winfo_ismapped())
            self.assertGreater(combo.winfo_width(), 80)
            self.assertLessEqual(combo.winfo_rootx() + combo.winfo_width(),
                                 self.app._input_row.winfo_rootx() + self.app._input_row.winfo_width())
            self.assertLessEqual(combo.winfo_rooty() + combo.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
