"""Slow disk/process/market operations must not occupy the Tk thread."""
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
import brand_marks
from plugin_market import Marketplace, build_tool_catalog
import test_layout_dpi as layout


class StartupResponsivenessTests(unittest.TestCase):
    def test_hidden_stub_views_are_built_once_on_first_visit(self):
        with patch.object(gui.ForgeGuiApp, "_build_stub_view", autospec=True) as build:
            with layout.isolated_app() as (root, app, errors):
                build.assert_not_called()
                frame = app._views["knowledge"]
                app._show_view("knowledge")
                app._return_to_chat()
                app._show_view("knowledge")
                build.assert_called_once_with(app, frame, "knowledge")
                self.assertIs(app._views["knowledge"], frame)
                self.assertEqual(errors, [])

    def test_brand_image_belongs_to_requested_tk_interpreter(self):
        import tkinter as tk
        first, second = tk.Tk(), tk.Tk()
        first.withdraw()
        second.withdraw()
        first_destroyed = False
        try:
            image, keep = brand_marks.mark_icon("mimo", 16, master=second)
            self.assertIsNotNone(image)
            self.assertIs(image.tk, second.tk)
            tk.Label(second, image=image)
            other, _ = brand_marks.mark_icon("mimo", 16, master=first)
            self.assertIs(other.tk, first.tk)
            self.assertIsNot(other, image)
            first.destroy()
            first_destroyed = True
            image2, _ = brand_marks.mark_icon("mimo", 16, master=second)
            tk.Label(second, image=image2)
        finally:
            if not first_destroyed:
                first.destroy()
            second.destroy()
            brand_marks.clear_cache()

    def test_startup_history_read_is_off_tk_thread(self):
        threads = []
        release = threading.Event()

        def read(app):
            threads.append(threading.get_ident())
            release.wait(.7)
            return []

        try:
            with patch.object(gui.ForgeGuiApp, "_load_sessions", read):
                with layout.isolated_app() as (root, app, errors):
                    self.assertTrue(threads)
                    self.assertNotIn(threading.get_ident(), threads)
                    heartbeat = []
                    root.after(1, lambda: heartbeat.append(True))
                    layout.pump(root, .05)
                    self.assertTrue(heartbeat)
                    release.set()
                    layout.pump(root)
                    self.assertEqual(errors, [])
        finally:
            release.set()

    def test_free_port_process_creation_is_off_tk_thread(self):
        with layout.isolated_app() as (root, app, errors):
            app.run_py = Path(gui.__file__).resolve().parents[1] / "run.py"
            app.user_rows = [{"id": "test", "config": {
                "wire": "openai", "baseURL": "https://example.invalid/v1",
                "model": "default", "apiKey": "test-only"}}]
            app.model_var.set("default")
            release, entered = threading.Event(), threading.Event()
            proc = Mock()

            def spawn(*args, **kwargs):
                entered.set()
                release.wait(.7)
                return proc

            try:
                with patch.object(gui, "port_in_use", return_value=False), \
                     patch.object(gui, "bindable_gateway_port", side_effect=lambda port: port), \
                     patch.object(gui.subprocess, "Popen", side_effect=spawn), \
                     patch.object(app, "_gateway_started") as started:
                    begin = time.monotonic()
                    self.assertTrue(app._start_gateway())
                    self.assertLess(time.monotonic() - begin, .2)
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(app._gateway_starting)
                    root.after(1, lambda: app.status_var.set("heartbeat"))
                    layout.pump(root, .05)
                    self.assertEqual(app.status_var.get(), "heartbeat")
                    release.set()
                    layout.pump(root)
                    started.assert_called_once_with(proc, app.user_rows[0]["config"])
                    self.assertEqual(errors, [])
            finally:
                release.set()
                app.gateway_proc = None

    def test_market_scan_does_not_block_and_refreshes_are_coalesced(self):
        with layout.isolated_app() as (root, app, errors):
            release, entered = threading.Event(), threading.Event()
            market = Mock()

            def scan():
                entered.set()
                release.wait(.7)
                return []

            market.catalog.side_effect = scan
            market.summary.return_value = dict(enabled=0, installed=0, total=0,
                                               region="test", sources=[])
            app._build_market_panel(app.market_holder)
            app._market_built = True
            try:
                with patch.object(app, "_market_obj", return_value=market), \
                     patch.object(app, "_plugin_runtime_obj", return_value=None), \
                     patch.object(app, "_gateway_tool_names", return_value=([], "")):
                    begin = time.monotonic()
                    app._refresh_market()
                    self.assertLess(time.monotonic() - begin, .2)
                    self.assertTrue(entered.wait(1))
                    for value in ("a", "b", "final"):
                        app.market_query_var.set(value)
                    self.assertEqual(market.catalog.call_count, 1)
                    release.set()
                    layout.pump(root, .3)
                    self.assertIn("共 0 条", app.market_stat_var.get())
                    self.assertLessEqual(market.catalog.call_count, 2)
                    self.assertEqual(errors, [])
            finally:
                release.set()

    def test_market_mutation_keeps_tk_alive_and_cannot_double_submit(self):
        with layout.isolated_app() as (root, app, errors):
            release, entered = threading.Event(), threading.Event()
            market = Mock()

            def disable(pid):
                entered.set()
                release.wait(.7)

            market.disable.side_effect = disable
            try:
                with patch.object(app, "_market_obj", return_value=market):
                    begin = time.monotonic()
                    app._market_action("test-plugin", "disable")
                    self.assertLess(time.monotonic() - begin, .2)
                    self.assertTrue(entered.wait(1))
                    app._market_action("test-plugin", "disable")
                    root.after(1, lambda: app.status_var.set("heartbeat"))
                    layout.pump(root, .05)
                    self.assertEqual(app.status_var.get(), "heartbeat")
                    release.set()
                    layout.pump(root)
                    market.disable.assert_called_once_with("test-plugin")
                    self.assertFalse(app._market_action_busy)
                    self.assertEqual(errors, [])
            finally:
                release.set()

    def test_team_switch_changes_actual_plan_and_reports_failed_persistence(self):
        with layout.isolated_app() as (root, app, errors):
            cfg = {"cluster": {"enabled": True, "count": 2}, "memory_mode": "isolated"}
            with patch.object(gui, "save_desktop_config", return_value=False):
                app.input_card.team_pill.set_mode("on")
            self.assertEqual(app._plan_sidecars(cfg, "你好")["kind"], "cluster")
            self.assertIn("未保存", app.status_var.get())
            app.input_card.team_pill.set_mode("off")
            self.assertIsNone(app._plan_sidecars(cfg, "审查代码"))
            app.input_card.team_pill.set_mode("auto")
            self.assertIsNone(app._plan_sidecars(cfg, "你好"))
            self.assertEqual(app._plan_sidecars(cfg, "审查代码")["kind"], "cluster")
            self.assertEqual(errors, [])

    def test_saved_team_mode_controls_first_plan(self):
        with patch.object(gui.ForgeGuiApp, "_load_team_mode", return_value="on"):
            with layout.isolated_app() as (root, app, errors):
                cfg = {"cluster": {"enabled": True, "count": 2}}
                self.assertEqual(app.input_card.team_pill.mode(), "on")
                self.assertIsNotNone(app._plan_sidecars(cfg, "你好"))
                self.assertEqual(errors, [])

    def test_trust_prompt_is_on_tk_and_keeps_reviewed_fingerprint(self):
        with layout.isolated_app() as (root, app, errors):
            market, plugin = Mock(), Mock(installed=True, error="", version="1")
            plugin.ack_of.return_value = "reviewed-files"
            plugin.permission_labels.return_value = ["filesystem"]
            market.find.return_value = plugin
            prompt_threads = []

            def confirm(*args, **kwargs):
                prompt_threads.append(threading.get_ident())
                return True

            with patch.object(app, "_market_obj", return_value=market), \
                 patch.object(gui.messagebox, "askyesno", side_effect=confirm):
                app._market_action("test-plugin", "ack")
                layout.pump(root, .2)
                market.ack.assert_called_once_with("test-plugin", expected_fingerprint="reviewed-files")
                self.assertEqual(prompt_threads, [threading.get_ident()])
                self.assertFalse(app._market_action_busy)
                self.assertEqual(errors, [])

    def test_failed_mutation_releases_busy_state_for_retry(self):
        with layout.isolated_app() as (root, app, errors):
            market = Mock()
            market.disable.side_effect = [OSError("locked"), None]
            with patch.object(app, "_market_obj", return_value=market):
                app._market_action("test-plugin", "disable")
                layout.pump(root)
                self.assertFalse(app._market_action_busy)
                self.assertIn("locked", app.status_var.get())
                app._market_action("test-plugin", "disable")
                layout.pump(root)
                self.assertEqual(market.disable.call_count, 2)
                self.assertIn("已禁用", app.status_var.get())
                self.assertEqual(errors, [])

    def test_repeated_history_refresh_discards_old_snapshot(self):
        with layout.isolated_app() as (root, app, errors):
            release, entered = threading.Event(), threading.Event()
            calls = []

            def read():
                calls.append(True)
                if len(calls) == 1:
                    entered.set()
                    release.wait(.7)
                return [{"id": str(len(calls)), "title": "old" if len(calls) == 1 else "fresh"}]

            try:
                with patch.object(app, "_load_sessions", side_effect=read), \
                     patch.object(app, "_render_history") as render:
                    app._refresh_history()
                    self.assertTrue(entered.wait(1))
                    for _ in range(10):
                        app._refresh_history()
                    release.set()
                    layout.pump(root, .2)
                    self.assertEqual(len(calls), 2)
                    render.assert_called_once_with([{"id": "2", "title": "fresh"}])
                    self.assertEqual(errors, [])
            finally:
                release.set()

    def test_tool_query_uses_short_timeout_without_mutating_chat_timeout(self):
        with layout.isolated_app() as (root, app, errors):
            timeout = app.client.timeout
            with patch.object(app, "_port_open", return_value=True), \
                 patch.object(app.client, "list_tools", return_value=[]) as query:
                self.assertEqual(app._gateway_tool_names(), ([], ""))
                query.assert_called_once_with(timeout=3.0)
                self.assertEqual(app.client.timeout, timeout)

    def test_market_summary_and_tool_view_reuse_same_snapshot(self):
        # These are display snapshots; executable trust checks remain in runtime.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            market = Marketplace(home=Path(tmp))
            items = market.catalog()
            expected = build_tool_catalog([], market)
            with patch.object(market, "catalog", side_effect=AssertionError("rescan")):
                self.assertEqual(market.summary(items=items)["total"], len(items))
                self.assertEqual(build_tool_catalog([], market, plugins=items), expected)

    def test_provider_filter_selects_original_row_and_clear_restores_list(self):
        with layout.isolated_app() as (root, app, errors):
            app.user_rows = [{"id": name, "config": {
                "baseURL": "https://example.invalid/v1", "model": name}}
                for name in ("first", "second", "third")]
            app._refresh_provider_list()
            app._provider_search_focus_in()
            app.provider_search_var.set("third")
            app.provider_list.selection_set(0)
            app._on_provider_select()
            self.assertEqual(json.loads(app.input_text.get("1.0", "end"))["id"], "third")
            app.provider_search_var.set("")
            app._provider_search_focus_out()
            self.assertEqual(app.provider_list.size(), 3)
            self.assertEqual(errors, [])

    def test_empty_provider_search_result_cannot_edit_first_row(self):
        with layout.isolated_app() as (root, app, errors):
            app._provider_search_focus_in()
            app.provider_search_var.set("does-not-exist")
            before = app.input_text.get("1.0", "end")
            app.provider_list.selection_set(0)
            app._on_provider_select()
            self.assertEqual(app.input_text.get("1.0", "end"), before)
            self.assertEqual(errors, [])

    def test_new_provider_clears_previous_save_and_model_edit_target(self):
        with layout.isolated_app() as (root, app, errors):
            app.provider_list.selection_set(0)
            app._on_provider_select()
            app._pending_rows = [app.user_rows[0]]
            app._organized_input = app.input_text.get("1.0", "end-1c").strip()
            app.save_btn.configure(state="normal")
            app._new_provider()
            self.assertEqual(app._pending_rows, [])
            self.assertEqual(app._organized_input, "")
            self.assertEqual(str(app.save_btn["state"]), "disabled")
            self.assertIsNone(app._model_edit_row_id)
            self.assertEqual(app.provider_list.curselection(), ())
            self.assertTrue(app._editor_is_dirty())
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
