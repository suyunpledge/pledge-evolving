"""Interrupted visual batch: real-widget lifecycle, overflow and action guards."""
import _tkinter
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from gui_theme import apply_ttk_theme
from .agent_panel import Conversation
from . import test_app


class ConversationVisualTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self._initial_scaling = self.root.tk.call('tk', 'scaling')
        self.addCleanup(self.cleanup_tk)
        self.root.attributes('-alpha', 0)
        self.root.geometry('480x260')
        apply_ttk_theme(self.root)
        self.errors = []
        self.root.report_callback_exception = lambda *args:self.errors.append(args)
        self.chat = Conversation(self.root, lambda path:None)
        self.chat.pack(fill='both', expand=True)
        self.flush()

    def cleanup_tk(self):
        import gc
        self.root.tk.call('tk', 'scaling', self._initial_scaling)
        self.root.destroy()
        self.chat = None
        self.root = None
        gc.collect()  # Tcl-owned objects must be collected on this thread.

    def flush(self):
        self.root.update_idletasks()
        for _ in range(32):
            if not self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                break
        self.assertEqual(self.errors, [])

    def test_empty_card_message_clear_cycles_have_one_overlay_and_no_fake_message(self):
        for _ in range(3):
            self.flush()
            self.assertTrue(self.chat._empty_card.winfo_ismapped())
            self.assertEqual(self.chat.get('1.0', 'end-1c'), '')
            old = self.chat._empty_card
            self.chat.message('Agent', 'Actual message')
            self.assertIsNone(self.chat._empty_card)
            self.assertFalse(old.winfo_exists())
            self.assertIn('Actual message', self.chat.get('1.0', 'end'))
            self.chat.clear()

    def test_plain_recovery_insert_also_removes_empty_overlay(self):
        self.chat.configure(state='normal')
        self.chat.insert('end', 'Visible plain content')
        self.chat.configure(state='disabled')
        self.flush()
        self.assertIsNone(self.chat._empty_card)
        self.assertIn('Visible plain content', self.chat.get('1.0', 'end'))

    def test_ignored_insert_into_disabled_text_keeps_the_empty_card(self):
        card = self.chat._empty_card
        self.chat.insert('end', 'ignored by disabled Text')
        self.flush()
        self.assertEqual(self.chat.get('1.0', 'end-1c'), '')
        self.assertIs(self.chat._empty_card, card)

    def test_empty_card_fits_narrow_panels_at_three_font_scales(self):
        for scale in (1, 1.5, 2):
            self.root.tk.call('tk', 'scaling', scale * 96 / 72)
            for width, height in ((210, 160), (480, 260), (1000, 300)):
                self.root.geometry(f'{width}x{height}')
                self.chat.clear()
                self.flush()
                card = self.chat._empty_card
                self.assertGreaterEqual(card.winfo_x(), 0)
                self.assertGreaterEqual(card.winfo_y(), 0)
                self.assertLessEqual(card.winfo_x()+card.winfo_width(), self.chat.winfo_width())
                self.assertLessEqual(card.winfo_y()+card.winfo_height(), self.chat.winfo_height())

    def test_markdown_rule_resizes_and_is_destroyed_with_history(self):
        self.chat.message('Agent', 'before\n\n---\n\nafter')
        self.flush()
        rules = [w for w in self.chat._windows if getattr(w, '_conversation_rule', False)]
        self.assertEqual(len(rules), 1)
        line = rules[0]
        before = line.winfo_width()
        self.root.geometry('300x260')
        self.flush()
        self.assertLess(line.winfo_width(), before)
        self.assertNotIn('────────', self.chat.get('1.0', 'end'))
        self.assertIn('after', self.chat.get('1.0', 'end'))
        self.chat.clear()
        self.assertFalse(line.winfo_exists())

    def test_idle_message_during_empty_resize_does_not_touch_destroyed_card(self):
        self.chat.after_idle(lambda:self.chat.message('Agent', 'Arrived during resize'))
        self.chat._empty_size = None
        self.chat._fit_empty_hint(300, 160)
        self.flush()
        self.assertIsNone(self.chat._empty_card)
        self.assertIn('Arrived during resize', self.chat.get('1.0', 'end'))

    def test_welcome_cards_keep_their_content_height_and_open_callback(self):
        from .welcome import WelcomeView
        self.chat.pack_forget()
        opened = []
        welcome = WelcomeView(self.root, on_open=lambda:opened.append(True),
                              on_recent=lambda path:None, recent_provider=lambda:[])
        welcome.pack(fill='both', expand=True)
        self.root.geometry('700x650')
        self.flush()
        for card in welcome.action_cards:
            self.assertGreater(card.winfo_height(), 60)
            label = card.desc_label
            self.assertTrue(label.winfo_ismapped())
            self.assertLessEqual(label.winfo_rooty()+label.winfo_height(),
                                 card.winfo_rooty()+card.winfo_height())
        welcome.open_button.invoke()
        self.assertEqual(opened, [True])


class DialogVisualTests(unittest.TestCase):
    setUp = test_app.WorkbenchTests.setUp
    cleanup_tk = test_app.WorkbenchTests.cleanup_tk
    step = test_app.WorkbenchTests.step
    pump = test_app.WorkbenchTests.pump

    def window(self):
        return next(w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel))

    def children(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.children(child)

    def test_recent_project_button_retains_its_navigation_callback(self):
        self.app.recent_projects()
        window = self.window()
        button = next(w for w in self.children(window) if isinstance(w, tk.Button)
                      and w.cget('text') == str(self.project.resolve()))
        with patch.object(self.app, 'open_project') as opened:
            button.invoke()
        opened.assert_called_once_with(str(self.project.resolve()))
        self.assertFalse(window.winfo_exists())

    def test_role_dialog_save_is_reachable_and_keeps_role_save_behavior(self):
        self.app.model_settings()
        self.step()
        window = self.window()
        save = next(w for w in self.children(window) if isinstance(w, tk.Button) and w.cget('text') == '保存')
        self.pump(save.winfo_ismapped)
        self.assertLessEqual(save.winfo_y()+save.winfo_height(), window.winfo_height())
        save.invoke()
        self.assertFalse(window.winfo_exists())
        self.assertIn('已保存', self.app.status.get())

    def test_checkpoint_restore_remains_disabled_before_review(self):
        self.app.open_file(self.project/'one.py')
        self.pump(lambda:bool(self.app.documents))
        with patch.object(self.app.runtime, 'checkpoint_list', return_value=[]):
            self.app.checkpoints()
            self.pump(lambda:any(isinstance(w, tk.Toplevel) for w in self.app.winfo_children()))
        window = self.window()
        restore = next(w for w in self.children(window) if isinstance(w, tk.Button)
                       and w.cget('text') == '恢复已审查版本')
        self.assertEqual(str(restore.cget('state')), 'disabled')
        with patch.object(self.app, 'restore_reviewed_checkpoint') as action:
            restore.invoke()
        action.assert_not_called()

    def test_task_completion_and_background_refresh_restore_send_label(self):
        from forge.test_task_planning import Router
        self.app.runtime.bridge.router = Router(['done'])
        self.app.runtime.bridge.agent.thinking = False
        self.app.runtime.bridge.agent.thinking_mode = 'off'
        self.app.planning.set('none')
        self.app.input.insert('1.0', 'Finish this task')
        self.app.send_button.invoke()
        self.pump(lambda:bool(self.app.runtime.tasks) and not self.app._submission_active
                  and self.app._jobs == 0)
        self.assertFalse(self.app.runtime.busy)
        self.assertEqual(self.app.send_button.cget('text'), '发送')


if __name__ == '__main__':
    unittest.main()
