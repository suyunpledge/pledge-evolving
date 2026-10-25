import threading
import json
import time
import unittest
from unittest.mock import patch

from test_layout_dpi import isolated_app, pump
from forge.tools import ToolResult
from forge_client import CompletionResult


class BrowserPanelTests(unittest.TestCase):
    def test_closed_browser_can_be_reopened_from_same_panel_without_restoring_grant(self):
        with isolated_app() as (root, app, errors):
            app._open_browser_panel()
            panel = app.browser_panel
            self.addCleanup(panel.close)
            previous = panel.current
            panel.enabled.set(True); panel._grant()
            panel.revoke()
            with patch('forge.browser.BrowserSession.call', return_value=ToolResult(True)) as call:
                panel.navigate()
                deadline = time.monotonic() + 2
                while panel.busy and time.monotonic() < deadline:
                    pump(root, .03)
            self.assertFalse(panel.busy)
            self.assertIsNot(panel.current, previous)
            self.assertEqual(call.call_count, 1)
            self.assertFalse(panel.granted)
            self.assertFalse(errors)

    def test_full_capacity_keeps_cleanup_controls_available_and_allows_recovery(self):
        with isolated_app() as (root, app, errors):
            app._open_browser_panel()
            panel = app.browser_panel
            self.addCleanup(panel.close)
            workspace, mode, conversation = panel.key
            for i in range(3):
                panel.manager.session(workspace, mode, 'other-' + str(i))
            app._session_id = 'fifth-conversation'
            panel.open()
            self.assertTrue(panel.window.winfo_exists())
            self.assertIsNone(panel.current)
            panel.navigate()
            self.assertFalse(panel.busy)
            self.assertIn('关闭全部', panel.status.get())
            panel.close_all()
            deadline = time.monotonic() + 3
            while panel.busy and time.monotonic() < deadline:
                pump(root, .03)
            self.assertFalse(panel.busy)
            panel.open()
            self.assertIsNotNone(panel.current)
            self.assertFalse(errors)

    def test_stale_panel_cannot_navigate_or_grant_another_conversation(self):
        with isolated_app() as (root, app, errors):
            app._open_browser_panel()
            panel = app.browser_panel
            self.addCleanup(panel.close)
            panel.enabled.set(True); panel._grant()
            original_grant = panel.granted[panel.key]
            app._session_id = 'new-conversation'
            with patch.object(panel.current, 'call') as call:
                panel.navigate()
                panel.enabled.set(False); panel._grant()
                self.assertTrue(original_grant.is_set())
                panel.enabled.set(True); panel._grant()
                call.assert_not_called()
            self.assertFalse(panel.granted)
            self.assertFalse(panel.enabled.get())
            self.assertIn('已切换', panel.status.get())
            self.assertFalse(errors)

    def test_send_dispatches_browser_tools_and_stops_on_unknown_receipt(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), isolated_app() as (root, app, errors):
                app._open_browser_panel()
                panel = app.browser_panel
                self.addCleanup(panel.close)
                panel.enabled.set(True); panel._grant()
                calls = []
                def stream(messages, **options):
                    calls.append(messages[:])
                    self.assertIn('browser_observe', [t['function']['name'] for t in options['tools']])
                    if len(calls) == 1:
                        return CompletionResult('', tool_calls=[{'id':'observe-1', 'type':'function',
                            'function':{'name':'browser_observe', 'arguments':'{}'}}])
                    options['on_chunk']('Browser ready')
                    return CompletionResult('Browser ready')
                receipt = (ToolResult(False, error='Lost receipt', meta={'execution_state':'unknown'}) if unknown else
                    ToolResult(True, json.dumps({'page_id':'p','observation_id':'o','elements':[], 'url':'about:blank'})))
                with patch.object(app.client, 'health', return_value=(True,'ok')), \
                     patch.object(app.client, 'list_tools', return_value=[]), \
                     patch.object(app.client, 'stream_chat', side_effect=stream), \
                     patch.object(app.client, 'call_tool') as gateway_call, \
                     patch.object(app, '_plan_sidecars', return_value=None), \
                     patch.object(app, '_read_planning_level', return_value='none'), \
                     patch.object(app, '_reload_plugin_tools', return_value=([], None)), \
                     patch.object(app.desktop_features, 'schemas', return_value=[]), \
                     patch.object(panel.current, 'call', return_value=receipt) as browser_call:
                    app.send_var.set('Inspect browser')
                    app._do_send()
                    deadline = time.monotonic()+8
                    while app._sending and time.monotonic() < deadline:
                        pump(root, .05)
                    self.assertFalse(app._sending, app.status_var.get())
                    self.assertEqual(browser_call.call_count, 1)
                    gateway_call.assert_not_called()
                    self.assertEqual(len(calls), 1 if unknown else 2)
                    if not unknown:
                        reply = json.loads(next(m.content for m in calls[-1] if m.role == 'tool'))
                        self.assertIn('action_id', reply['meta'])
                        self.assertIn('event_id', reply['meta'])
                panel.close()
                self.assertFalse(errors)

    def test_menu_opens_panel_and_mode_switch_preserves_browser_without_grant_leak(self):
        with isolated_app() as (root, app, errors):
            app._open_browser_panel()
            panel = app.browser_panel
            self.addCleanup(panel.close)
            work = panel.current
            panel.enabled.set(True); panel._grant()
            workspace = app._active_workspace()
            lane = panel.lane(workspace=workspace, rows=app.user_rows, scope=None, cancel=threading.Event())
            self.assertIsNotNone(lane)
            app._workspace_mode = app._workspace_kind.CODING
            panel.open()
            self.assertIsNot(work, panel.current)
            self.assertFalse(panel.enabled.get())
            self.assertFalse(work.revoked.is_set())
            self.assertIsNone(panel.lane(workspace=workspace, rows=app.user_rows, scope=None, cancel=None))
            panel.close()
            self.assertTrue(work.revoked.is_set())
            self.assertFalse(errors)

    def test_revocation_cancels_existing_lane_even_after_reauthorization(self):
        with isolated_app() as (root, app, errors):
            app._open_browser_panel()
            panel = app.browser_panel
            self.addCleanup(panel.close)
            panel.enabled.set(True); panel._grant()
            lane = panel.lane(workspace=app._active_workspace(), rows=app.user_rows, scope=None, cancel=None)
            panel.enabled.set(False); panel._grant()
            panel.enabled.set(True); panel._grant()
            self.assertTrue(lane.cancel.is_set())
            result = lane.call('browser_observe', {})
            self.assertFalse(result['ok'])
            self.assertEqual(result['meta']['execution_state'], 'not_started')
            panel.close()
            self.assertFalse(errors)


if __name__ == '__main__':
    unittest.main()
