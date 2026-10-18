"""Exercise the actual Coding send button, Tk callbacks and existing Runtime."""
import json
import threading
import unittest
from unittest.mock import patch

from forge.model import Completion, Usage
from forge.test_task_planning import Router
from development_mode import test_app


class SubmissionTests(unittest.TestCase):
    setUp = test_app.WorkbenchTests.setUp
    cleanup_tk = test_app.WorkbenchTests.cleanup_tk
    step = test_app.WorkbenchTests.step
    pump = test_app.WorkbenchTests.pump

    def offline(self, router=None):
        self.app.planning.set('none')
        self.app.runtime.bridge.agent.thinking = False
        self.app.runtime.bridge.agent.thinking_mode = 'off'
        if router is not None:
            self.app.runtime.bridge.router = router

    def test_named_model_button_reaches_actual_transport(self):
        self.offline()
        calls = []
        answers = iter([
            '<tool_call>' + json.dumps({'tool': 'read_file', 'args': {'path': 'one.py'}}) + '</tool_call>',
            '<tool_call>' + json.dumps({'tool': 'write_file', 'args': {'path': 'one.py', 'content': 'print(2)\n'}}) + '</tool_call>',
            'submitted successfully'])
        class Transport:
            def complete(self, provider, model, messages, **options):
                calls.append((provider.name, model))
                return next(answers), Usage(10, 5, provider.name, model)
        self.app.runtime.bridge.router.transport = Transport()
        selected = self.app.catalog_rows[0]
        self.app._safe_set_routing_primary(selected['label'])
        self.app.permission.set('workspace-write')
        errors = []
        self.root.report_callback_exception = lambda kind, error, tb: errors.append(str(error))
        self.app.input.insert('1.0', 'Run the selected model')
        self.app.send_button.invoke()
        self.assertEqual(errors, [], 'Send must not disappear into a Tcl callback error')
        self.pump(lambda: bool(self.app.runtime.tasks) and self.app._jobs == 0)
        self.assertEqual(calls[0], self.app.runtime.catalog[selected['id']])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(pair == calls[0] for pair in calls))
        self.assertEqual((self.project / 'one.py').read_text(encoding='utf-8'), 'print(2)\n')
        self.assertEqual(self.app.runtime.last_snapshot.model, calls[0])
        chat = self.app.chat.get('1.0', 'end')
        self.assertEqual(chat.count('Run the selected model'), 1)

    def test_loaded_catalog_populates_real_model_dropdown(self):
        chain = self.app._runtime_ctx._routing
        choices = [w for w in chain.winfo_children() if w.winfo_class() == 'TCombobox']
        values = tuple(choices[0].cget('values'))
        self.assertTrue(set(row['label'] for row in self.app.catalog_rows).issubset(values))

    def test_not_ready_has_visible_reason_and_preserves_draft(self):
        self.app.runtime.ready = False
        self.app.input.insert('1.0', 'draft must stay')
        # Readiness now offers same-project initialization instead of leaving
        # the user at a dead-end notice. Simulate a refused/failed retry.
        with patch.object(self.app, 'open_project', return_value=False):
            self.app.send_button.invoke()
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'draft must stay')
        self.assertIn('无法发送', self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.log_tabs.select(), str(self.app.chat.master))
        self.assertEqual(self.app.runtime.tasks, {})

    def test_unknown_model_does_not_silently_fall_back(self):
        self.offline(Router(['must not run']))
        self.app._safe_set_routing_primary('removed / model')
        self.app.input.insert('1.0', 'keep this draft')
        self.app.send_button.invoke()
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'keep this draft')
        self.assertIn('模型', self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.runtime.tasks, {})

    def test_permission_display_matches_actual_initial_grant(self):
        self.assertEqual(self.app.permission.get(), 'read-only')
        self.assertEqual(self.app._runtime_ctx.get_policy()['sandbox'], 'read-only')

    def test_role_control_updates_the_snapshot_source(self):
        self.app._on_role_change('simple')
        self.assertEqual(self.app.task_role.get(), 'simple')

    def blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class Blocking(Router):
            def complete(inner, messages, **options):
                if not inner.calls:
                    entered.set()
                    if not release.wait(15):
                        raise RuntimeError('test gate timed out')
                return super().complete(messages, **options)
        self.offline(Blocking(['first finished', 'second finished']))
        self.app.input.insert('1.0', 'first request')
        self.app.send_button.invoke()
        self.pump(entered.is_set)
        return release

    def test_busy_button_accepts_queue_and_next_dispatch_preserves_new_typing(self):
        release = self.blocked()
        self.assertEqual(str(self.app.send_button.cget('state')), 'normal')
        self.app.input.insert('1.0', 'second request')
        self.app.send_button.invoke()
        self.assertEqual(len(self.app._pending_prompts), 1)
        self.assertIn('排队', self.app.chat.get('1.0', 'end'))
        self.app.input.insert('1.0', 'third unsent draft')
        release.set()
        self.pump(lambda: len(self.app.runtime.tasks) == 2 and self.app._jobs == 0)
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'third unsent draft')

    def test_queue_capacity_rejection_preserves_unsent_draft(self):
        release = self.blocked()
        for i in range(3):
            self.app.input.insert('1.0', 'queued ' + str(i))
            self.app.send_button.invoke()
        self.app.input.insert('1.0', 'keep overflow draft')
        self.app.send_button.invoke()
        self.assertEqual(len(self.app._pending_prompts), 3)
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'keep overflow draft')
        self.assertIn('排队已满', self.app.chat.get('1.0', 'end'))
        self.app.cancel_task()
        release.set()
        self.pump(lambda: self.app._jobs == 0)

    def test_queue_notice_does_not_relabel_the_running_agent(self):
        release = self.blocked()
        with patch.object(self.app, 'set_agent_status', create=True) as status:
            self.app.input.insert('1.0', 'queued second')
            self.app.send_button.invoke()
            status.assert_not_called()
            self.app.cancel_task()
        release.set()
        self.pump(lambda: self.app._jobs == 0)

    def test_queued_request_keeps_its_model_role_permission_and_planning(self):
        release = self.blocked()
        self.app._on_role_change('simple')
        self.app.input.insert('1.0', 'second request')
        self.app.send_button.invoke()
        queued = self.app._pending_prompts[0]
        self.app.permission.set('workspace-write')
        self.app._on_role_change('complex')
        self.app.planning.set('high')
        self.app._safe_set_routing_primary('different / invalid-next-selection')
        release.set()
        self.pump(lambda: len(self.app.runtime.tasks) == 2 and self.app._jobs == 0)
        snapshots = [v['snapshot'] for v in self.app.runtime.tasks.values()]
        self.assertEqual(snapshots[-1].role, 'simple')
        self.assertEqual(snapshots[-1].permission, 'read-only')
        self.assertEqual(snapshots[-1].model, queued['snapshot'].model)
        self.assertEqual(self.app.runtime.bridge.agent.planning_level, 'none')

    def test_cancel_before_runtime_admission_stops_active_and_queued_requests(self):
        router = Router(['must never call the model'])
        self.offline(router)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.app.runtime.run
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(15):
                raise RuntimeError('test gate timed out')
            return original(*args, **kwargs)
        with patch.object(self.app.runtime, 'run', side_effect=delayed):
            self.app.input.insert('1.0', 'accepted but not started')
            self.app.send_button.invoke()
            self.pump(entered.is_set)
            self.assertFalse(self.app.runtime.busy)
            self.app.input.insert('1.0', 'queued second')
            self.app.send_button.invoke()
            self.assertEqual(len(self.app._pending_prompts), 1)
            self.app.input.insert('1.0', 'new unsent draft')
            self.app.cancel_task()
            self.assertEqual(self.app._pending_prompts, [])
            release.set()
            self.pump(lambda: self.app._jobs == 0)
        self.assertEqual(router.calls, [])
        self.assertEqual(self.app.task_evidence.phase, 'cancelled')
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'new unsent draft')

    def test_worker_launch_failure_is_visible_and_keeps_the_draft(self):
        self.offline(Router(['must not run']))
        self.pump(lambda: self.app._jobs == 0)
        self.app.input.insert('1.0', 'preserve failed submission')
        with patch.object(self.app, 'work', side_effect=RuntimeError('worker unavailable')):
            self.app.send_button.invoke()
        self.assertFalse(self.app._submission_active)
        self.assertIsNone(self.app._active_submission)
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), 'preserve failed submission')
        self.assertIn('任务未启动', self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.runtime.tasks, {})

    def test_oversized_prompt_does_not_disappear_before_runtime_validation(self):
        prompt = '任务' * 3000
        self.app.input.insert('1.0', prompt)
        self.app.send_button.invoke()
        self.assertEqual(self.app.input.get('1.0', 'end-1c'), prompt)
        self.assertIn('16 KiB', self.app.chat.get('1.0', 'end'))
        self.assertEqual(self.app.runtime.tasks, {})

    def test_preview_controls_cannot_claim_unsupported_host_permissions(self):
        policy = self.app._runtime_ctx._policy
        controls = [w for w in policy.winfo_children() if w.winfo_class() == 'TCombobox']
        self.assertEqual([str(w.cget('state')) for w in controls],
                         ['disabled', 'readonly', 'disabled', 'disabled'])
        self.assertEqual(tuple(controls[1].cget('values')), ('read-only', 'workspace-write'))
        policy._sandbox.set('workspace-write')
        policy._emit()
        self.assertEqual(self.app.permission.get(), 'workspace-write')
        self.assertEqual(self.app._runtime_ctx.get_policy()['mode'], 'acceptEdits')
        self.assertEqual(self.app.runtime.bridge.agent.policy.mode.value, 'read-only',
                         'Next-task choices must not mutate the current Agent')
        self.app._on_policy_change(mode='bypassPermissions', sandbox='danger-full-access',
                                   read_scope='all', secret_scope='shared')
        self.assertEqual(self.app.permission.get(), 'workspace-write')
        self.assertEqual(self.app._runtime_ctx.get_policy()['sandbox'], 'workspace-write')

    def test_queued_task_cannot_be_redirected_to_a_new_project(self):
        release = self.blocked()
        self.app.input.insert('1.0', 'queued for original project')
        self.app.send_button.invoke()
        original = self.app.source.root
        other = self.base / 'other-project'
        other.mkdir()
        self.app.open_project(other)
        self.assertEqual(self.app.source.root, original)
        self.assertFalse(self.app._loading)
        self.app.cancel_task()
        release.set()
        self.pump(lambda: self.app._jobs == 0)

    def test_session_switch_and_ancillary_actions_wait_for_pending_submission(self):
        self.pump(lambda: self.app._jobs == 0)
        self.app._pending_prompts.append(self.app._capture_submission('queued', False))
        with patch.object(self.app, 'work') as work:
            self.app._new_agent_conversation()
            self.app.restore_session(self.base / 'old.jsonl')
            self.app.run_tests()
            self.app.review()
        work.assert_not_called()

    def test_closing_workbench_cancels_delayed_refresh_without_touching_host_timers(self):
        self.pump(lambda: self.app._jobs == 0)
        self.app._schedule_refresh(30000)
        refresh = self.app._refresh_after_id
        host_timer = self.root.after(30000, lambda: None)
        self.app.destroy()
        timers = set(self.root.tk.splitlist(self.root.tk.call('after', 'info')))
        self.assertNotIn(refresh, timers)
        self.assertIn(host_timer, timers)
        self.root.after_cancel(host_timer)


if __name__ == '__main__':
    unittest.main()
