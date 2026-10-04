"""Marketplace entry, lifecycle labels and return preserve the main layout."""
import json
from contextlib import contextmanager
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
import plugin_capabilities as pc
import plugin_market as pm
import test_layout_dpi as layout
import test_navigation as navigation


@contextmanager
def market_app(*args, **kwargs):
    with layout.isolated_app(*args, **kwargs) as (root, app, errors):
        try:
            yield root, app, errors
        finally:
            # Wait for actual completion, not a fixed sleep: Windows cannot
            # delete the temporary home while a scan still holds state.lock.
            deadline = time.monotonic() + 5
            while any(job["running"] for job in app._background_jobs.values()) and time.monotonic() < deadline:
                layout.pump(root, .02)
            if any(job["running"] for job in app._background_jobs.values()):
                raise AssertionError("Marketplace background operation did not finish")


class MarketplaceUiTests(unittest.TestCase):
    def test_grant_action_reaches_reviewed_capability_dialog(self):
        with market_app() as (root, app, errors):
            plugin = pm.parse_manifest({"id": "demo", "capabilities": ["repo.read"]})
            plugin.installed = plugin.enabled = plugin.acked = True
            market = Mock()
            market.find.return_value = plugin
            with patch.object(app, "_market_obj", return_value=market), \
                 patch.object(app, "_show_grant_dialog") as show:
                app._market_action("demo", "grant")
                layout.pump(root, .3)
                show.assert_called_once_with(plugin, plugin.ack_of())
                self.assertFalse(app._market_action_busy)
            self.assertEqual(errors, [])

    def test_revoke_action_reaches_marketplace_and_finishes(self):
        with market_app() as (root, app, errors):
            market = Mock()
            with patch.object(app, "_market_obj", return_value=market), \
                 patch.object(app, "_refresh_market") as refresh:
                app._market_action("demo", "revoke")
                layout.pump(root, .3)
                market.revoke_grants.assert_called_once_with("demo")
                refresh.assert_called_once_with()
                self.assertFalse(app._market_action_busy)
            self.assertEqual(errors, [])

    def test_main_entry_opens_market_and_returns_to_identical_layout(self):
        for scale in layout.SCALES:
            with self.subTest(scale=scale), market_app(scale, (1920, 1000)) as (root, app, errors):
                with patch.object(app, "_gateway_tool_names", return_value=([], "offline")):
                    for opened in (False, True):
                        if opened:
                            app._open_workspace()
                            layout.pump(root)
                            app.split.sash_place(0, app.split.winfo_width() - 540, 0)
                        layout.pump(root)
                        before = navigation.NavigationTests.main_layout(app)
                        app.market_entry_btn.invoke()
                        layout.pump(root, .2)
                        self.assertEqual(app._active_view, "tools")
                        self.assertEqual(app._tools_tab, "market")
                        self.assertTrue(app.market_holder.winfo_ismapped())
                        app.view_back_btn.invoke()
                        layout.pump(root)
                        self.assertEqual(navigation.NavigationTests.main_layout(app), before, opened)
                self.assertEqual(errors, [])

    def test_production_runtime_never_uses_legacy_python_worker(self):
        with market_app() as (root, app, errors):
            self.assertIsInstance(app._plugin_runtime_obj(), pc.CapabilityRuntime)
            self.assertEqual(errors, [])

    def test_failed_runtime_creation_does_not_abort_normal_chat(self):
        with market_app() as (root, app, errors):
            with patch.object(app, "_plugin_runtime_obj", side_effect=RuntimeError("host module unavailable")):
                self.assertEqual(app._reload_plugin_tools(), ([], None))
            self.assertEqual(errors, [])

    def test_new_dialogs_do_not_reuse_grant_scope_when_clock_matches(self):
        with market_app() as (root, app, errors):
            with patch.object(gui.time, "time", return_value=123.456):
                app._new_session()
                first = app._session_id
                app._new_session()
                self.assertNotEqual(app._session_id, first)
            self.assertEqual(errors, [])

    def test_grant_dialog_has_individual_choices_and_explicit_scope(self):
        with market_app() as (root, app, errors):
            plugin = pm.parse_manifest({"id": "demo", "capabilities": ["repo.read", "repo.write", "process.exec"]})
            plugin.installed = plugin.enabled = plugin.acked = True
            plugin.ack_fingerprint = plugin.ack_of()
            app._show_grant_dialog(plugin, plugin.ack_of())
            dialog = app._grant_dialog
            self.assertEqual(set(dialog.capability_vars), {"repo.read", "repo.write"})
            self.assertFalse(any(v.get() for v in dialog.capability_vars.values()))
            dialog.scope_var.set("session")
            dialog.capability_vars["repo.read"].set(True)
            with patch.object(app, "_apply_market_grant") as apply:
                dialog.grant_btn.invoke()
                apply.assert_called_once_with("demo", ["repo.read"], "session", plugin.ack_of(),
                                              workspace=app._repo_root(), session_id=app._session_id)
            self.assertEqual(errors, [])

    def test_pending_confirmation_is_not_a_trust_or_execution_grant(self):
        with market_app() as (root, app, errors):
            app._build_market_panel(app.market_holder)
            p = pm.parse_manifest({"id": "demo", "name": "Demo", "capabilities": ["repo.read"]})
            p.installed = True
            app._market_plugin_card(p)
            labels = [w.cget("text") for w in layout.descendants(app.market_list)
                      if w.winfo_class() in ("Label", "Button")]
            self.assertIn("知悉确认", labels)
            self.assertNotIn("确认信任", labels)
            self.assertTrue(any("未授权" in text for text in labels))
            self.assertEqual(errors, [])

    def test_scope_inheritance_is_visible_and_each_scope_edits_its_own_grant(self):
        with market_app() as (root, app, errors):
            p = pm.parse_manifest({"id": "demo", "capabilities": ["repo.read", "repo.write"]})
            p.grants = [{"workspace": pm.workspace_key(app._repo_root()), "session": "",
                         "fingerprint": p.ack_of(), "capabilities": ["repo.read", "repo.write"]}]
            app._show_grant_dialog(p, p.ack_of())
            dialog = app._grant_dialog
            self.assertFalse(dialog.capability_vars["repo.write"].get())
            labels = [w.cget("text") for w in layout.descendants(dialog) if w.winfo_class() == "Label"]
            self.assertTrue(any("继承工作区授权" in text for text in labels))
            dialog.scope_var.set("workspace")
            self.assertTrue(dialog.capability_vars["repo.write"].get())
            dialog.scope_var.set("session")
            self.assertFalse(dialog.capability_vars["repo.write"].get())
            dialog.destroy()
            self.assertEqual(errors, [])

    def test_background_grant_pins_workspace_session_and_fingerprint(self):
        with market_app() as (root, app, errors):
            market = Mock()
            with patch.object(app, "_market_obj", return_value=market):
                app._apply_market_grant("demo", ["repo.read"], "session", "reviewed")
                layout.pump(root, .2)
                market.grant.assert_called_once_with("demo", ["repo.read"], app._repo_root(),
                    session=app._session_id, expected_fingerprint="reviewed")
                self.assertFalse(app._market_action_busy)
            self.assertEqual(errors, [])

    def test_market_controls_fit_at_all_supported_scales_and_minimum_width(self):
        for scale in layout.SCALES:
            with self.subTest(scale=scale), market_app(scale, gui.MIN_SIZE) as (root, app, errors):
                with patch.object(app, "_gateway_tool_names", return_value=([], "offline")):
                    app.market_entry_btn.invoke()
                    layout.pump(root, .3)
                buttons = {w.cget("text"): w for w in layout.descendants(app.market_holder)
                           if w.winfo_class() == "Button"}
                for name in ("打开插件目录", "刷新", "安装本地插件", "查看审计", "全部", "工具", "主题", "面板", "集成"):
                    self.assertTrue(buttons[name].winfo_ismapped(), (scale, name))
                    self.assertGreaterEqual(buttons[name].winfo_width(), buttons[name].winfo_reqwidth(), (scale, name))
                for widget in layout.descendants(app.market_holder):
                    if widget.winfo_class() == "Button" and widget.winfo_ismapped():
                        self.assertGreaterEqual(widget.winfo_rootx(), app.market_holder.winfo_rootx())
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                            app.market_holder.winfo_rootx() + app.market_holder.winfo_width(),
                            (scale, widget.cget("text")))
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
