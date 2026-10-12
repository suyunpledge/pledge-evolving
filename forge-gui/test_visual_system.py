"""Meaningful retention/ownership/keyboard/DPI checks on real Tk components."""
import gc
import time
import tkinter as tk
from tkinter import ttk
import _tkinter
import unittest

import gui_theme as theme
from visual_system import WorkspaceDeck, ModeSwitcher, WorkspaceKind, NoticeBanner, PermissionPrompt
from visual_system.contracts import NoticeSpec, PermissionSpec
from visual_system.demo import VisualSystemDemo


class VisualSystemTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry('1360x850')
        self.root.attributes('-alpha', 0)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        theme.apply_ttk_theme(self.root)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.root.destroy()
        gc.collect()
        self.assertFalse(self.errors, self.errors)

    def pump(self, until=lambda: True, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for _ in range(80):
                if not self.root.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                    break
            if until():
                return
            time.sleep(.005)
        self.fail('Tk condition timed out')

    def test_workspace_switch_retains_context_tabs_draft_layout_and_running_callback(self):
        demo = VisualSystemDemo(self.root)
        demo.pack(fill=tk.BOTH, expand=True)
        demo.buffers['work'].insert('end', ' work draft')
        work = demo.deck.frames[WorkspaceKind.WORK]
        demo.select('coding')
        self.pump()
        coding = demo.deck.frames[WorkspaceKind.CODING]
        editor = demo.buffers['runner.py *']
        editor.insert('end', '# unsaved coding draft\n')
        demo.editor_tabs.select(1)
        demo.coding_panes.sashpos(0, 230)
        self.pump()
        sash = demo.coding_panes.sashpos(0)
        before = demo.tick
        for _ in range(4):
            demo.select('work')
            demo.select('coding')
        self.pump(lambda: demo.tick > before)
        self.assertIs(demo.deck.frames[WorkspaceKind.WORK], work)
        self.assertIs(demo.deck.frames[WorkspaceKind.CODING], coding)
        self.assertIs(work.master, coding.master)
        self.assertIn('unsaved coding draft', editor.get('1.0', 'end'))
        self.assertTrue(editor.edit_modified())
        self.assertEqual(demo.editor_tabs.index('current'), 1)
        self.assertEqual(demo.coding_panes.sashpos(0), sash)
        self.assertIn('work draft', demo.buffers['work'].get('1.0', 'end'))
        self.assertEqual(demo.project_label.cget('text'), 'project-example')

    def test_failed_workspace_mount_keeps_current_view_and_state(self):
        def fail(parent):
            tk.Label(parent, text='partial construction').pack()
            raise ValueError('cannot mount')
        deck = WorkspaceDeck(self.root, {'work': lambda p: tk.Text(p).pack(), 'coding': fail})
        deck.pack(fill=tk.BOTH, expand=True)
        original = deck.select('work')
        self.pump()
        with self.assertRaises(ValueError):
            deck.select('coding')
        self.assertIs(deck.selected, WorkspaceKind.WORK)
        self.assertEqual(set(deck.frames), {WorkspaceKind.WORK})
        self.assertTrue(original.winfo_ismapped())

    def test_mode_entry_waits_for_host_acknowledgment_and_rejects_disabled_actions(self):
        requests = []
        switcher = ModeSwitcher(self.root, requests.append)
        switcher.pack()
        self.pump()
        button = switcher.buttons[WorkspaceKind.CODING]
        button.invoke()
        self.assertEqual(requests, [WorkspaceKind.CODING])
        self.assertEqual(switcher.selected, WorkspaceKind.WORK)
        switcher.set_selected('coding')
        switcher.request('coding')
        self.assertEqual(len(requests), 1)
        button.configure(state='disabled')
        button.invoke()
        self.assertEqual(len(requests), 1)

    def test_arrow_key_navigation_requests_other_workspace(self):
        requests = []
        switcher = ModeSwitcher(self.root, requests.append)
        switcher.pack()
        self.pump()
        switcher.buttons[WorkspaceKind.WORK].focus_force()
        switcher.buttons[WorkspaceKind.WORK].event_generate('<Right>')
        self.pump(lambda: bool(requests))
        self.assertEqual(requests, [WorkspaceKind.CODING])

    def test_notice_action_retains_origin_and_never_acts_as_approval(self):
        actions = []
        spec = NoticeSpec('warn', '权限请求', '范围：指定项目。等待批准。', WorkspaceKind.CODING, 'task-reference')
        notice = NoticeBanner(self.root, spec, on_open=actions.append)
        notice.pack(fill=tk.X)
        self.pump()
        button = next(w for w in notice.winfo_children() if isinstance(w, tk.Button))
        button.invoke()
        self.assertEqual(actions, [spec])
        self.assertEqual(actions[0].task_id, 'task-reference')
        self.assertEqual(actions[0].workspace, WorkspaceKind.CODING)

    def test_five_dpi_scales_keep_shared_entry_reachable_and_density_local(self):
        for scale in (1, 1.25, 1.5, 1.75, 2):
            self.root.tk.call('tk', 'scaling', scale * 96 / 72)
            theme.apply_ttk_theme(self.root)
            demo = VisualSystemDemo(self.root)
            demo.pack(fill=tk.BOTH, expand=True)
            demo.select('coding')
            self.pump()
            self.assertEqual(ttk.Style(self.root).lookup('Treeview', 'rowheight'), theme.ui_px(self.root, theme.CONTROL_HEIGHT))
            for button in demo.switcher.buttons.values():
                self.assertGreaterEqual(button.winfo_height(), theme.ui_px(self.root, 32))
                self.assertGreater(button.winfo_width(), 0)
                self.assertLess(button.winfo_rootx() + button.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
            demo.destroy()

    def test_return_to_workspace_restores_its_keyboard_focus(self):
        entries = {}
        def mount(kind):
            def build(parent):
                entries[kind] = tk.Entry(parent)
                entries[kind].pack()
            return build
        deck = WorkspaceDeck(self.root, {k: mount(k) for k in WorkspaceKind})
        deck.pack(fill=tk.BOTH, expand=True)
        deck.select('work')
        self.pump()
        entries[WorkspaceKind.WORK].focus_force()
        self.pump()
        deck.select('coding')
        self.pump()
        entries[WorkspaceKind.CODING].focus_force()
        deck.select('work')
        self.pump()
        self.assertIs(self.root.focus_get(), entries[WorkspaceKind.WORK])

    def test_mode_control_taking_focus_does_not_forget_editor_focus(self):
        demo = VisualSystemDemo(self.root)
        demo.pack(fill=tk.BOTH, expand=True)
        self.pump()
        draft = demo.buffers['work']
        draft.focus_force()
        self.pump()
        demo.switcher.buttons[WorkspaceKind.CODING].focus_force()
        demo.switcher.buttons[WorkspaceKind.CODING].invoke()
        self.pump()
        demo.buffers['runner.py *'].focus_force()
        self.pump()
        demo.switcher.buttons[WorkspaceKind.WORK].focus_force()
        demo.switcher.buttons[WorkspaceKind.WORK].invoke()
        self.pump()
        self.assertIs(self.root.focus_get(), draft)

    def test_permission_prompt_only_emits_explicit_scoped_intents(self):
        intents = []
        spec = PermissionSpec('request-ref', WorkspaceKind.CODING, 'task-ref', 'project-example', 'write_file', 'src/runner.py')
        prompt = PermissionPrompt(self.root, spec, lambda request, intent: intents.append((request, intent)))
        prompt.pack(fill=tk.X)
        self.pump()
        self.assertFalse(intents)
        prompt.buttons['allow_once'].invoke()
        self.assertEqual(intents, [(spec, 'allow_once')])
        prompt.set_pending(False)
        prompt.buttons['allow_once'].invoke()
        self.assertEqual(len(intents), 1)
