"""Large catalogs and Windows IME events must yield to Tk input dispatch."""
import time
import threading
import unittest
from unittest.mock import Mock, patch

import chat_widgets as cw
import plugin_market as pm
import forge_gui_v2 as gui
import test_layout_dpi as layout


def snapshot(count=500):
    items = [pm.parse_manifest({"id": f"demo-{i}", "name": f"Demo {i}"})
             for i in range(count)]
    return items, dict(enabled=0, installed=0, total=count, region="test"), [], "", {}


class MarketResponsivenessTests(unittest.TestCase):
    def test_rapid_page_reentry_uses_recent_snapshot_but_expired_snapshot_refreshes(self):
        with layout.isolated_app() as (root, app, errors):
            app._build_market_panel(app.market_holder)
            app._market_built = True
            app._apply_market_snapshot(snapshot(1))
            with patch.object(app, "_refresh_market") as refresh:
                for _ in range(5):
                    app._open_plugin_market()
                    app._return_to_chat()
                refresh.assert_not_called()
                app._market_loaded_at = time.monotonic() - 3
                app._open_plugin_market()
                refresh.assert_called_once_with()
            self.assertEqual(errors, [])

    def test_large_catalog_renders_only_one_page_and_yields(self):
        with layout.isolated_app() as (root, app, errors):
            app._build_market_panel(app.market_holder)
            app._market_built = True
            with patch.object(app, "_market_plugin_card") as card:
                app._render_market(snapshot())
                self.assertLessEqual(card.call_count, 1, "render callback must yield between cards")
                layout.pump(root, .3)
                self.assertGreater(card.call_count, 0)
                self.assertLessEqual(card.call_count, 12, "widget count must not grow with catalog size")
            self.assertEqual(errors, [])

    def test_search_uses_cached_snapshot_without_disk_or_gateway_scan(self):
        with layout.isolated_app() as (root, app, errors):
            app._show_view("tools")
            app._tools_tab = "market"
            app._build_market_panel(app.market_holder)
            app._market_built = True
            app._render_market(snapshot(2))
            layout.pump(root)
            with patch.object(app, "_submit_background") as submit:
                for query in ("d", "de", "demo-1"):
                    app.market_query_var.set(query)
                layout.pump(root, .25)
                submit.assert_not_called()
                self.assertEqual(app._market_page_items[0].id, "demo-1")
            self.assertEqual(errors, [])

    def test_real_large_market_remains_interactive_at_minimum_size_and_high_dpi(self):
        for scale in (1, 2):
            with self.subTest(scale=scale), layout.isolated_app(scale, gui.MIN_SIZE) as (root, app, errors):
                app._show_view("tools")
                app._tools_tab = "market"
                app._build_market_panel(app.market_holder)
                app._market_built = True
                app.market_holder.pack(fill="both", expand=True)
                app.feature_holder.pack_forget()
                beats = []
                def beat():
                    beats.append(time.monotonic())
                    root.after(20, beat)
                beat()
                data = snapshot(1000)
                app._render_market(data)
                layout.pump(root, .7)
                self.assertLess(len(list(layout.descendants(app.market_list))), 450)
                self.assertGreater(len(beats), 8)
                self.assertLess(max(b-a for a, b in zip(beats, beats[1:])), .8)
                app._market_page = 83
                app._render_market(data)
                layout.pump(root, .3)
                self.assertEqual([p.id for p in app._market_page_items],
                                 [f"demo-{i}" for i in range(996, 1000)])
                app.market_query_var.set("demo-999")
                app._return_to_chat()
                layout.pump(root, .2)
                self.assertIsNone(app._market_render_job)
                self.assertIsNone(app._market_filter_job)
                self.assertEqual(errors, [])

    def test_slow_remote_sync_does_not_hide_cached_entries_or_double_submit(self):
        with layout.isolated_app() as (root, app, errors):
            app._show_view("tools")
            app._tools_tab = "market"
            app._build_market_panel(app.market_holder)
            app._market_built = True
            entered, release = threading.Event(), threading.Event()
            market = Mock()
            def sync(**kwargs):
                entered.set()
                release.wait(2)
                return {}
            market.sync_sources.side_effect = sync
            data = snapshot(1)
            data[4]["needs_sync"] = True
            try:
                with patch.object(app, "_market_obj", return_value=market), \
                     patch.object(app, "_refresh_market"):
                    app._apply_market_snapshot(data)
                    self.assertTrue(entered.wait(1))
                    app._sync_remote_sources()
                    layout.pump(root, .2)
                    self.assertEqual(market.sync_sources.call_count, 1)
                    self.assertIn("Demo 0", [w.cget("text") for w in layout.descendants(app.market_list)
                                            if w.winfo_class() == "Label"])
                    app._return_to_chat()
                    release.set()
                    layout.pump(root, .2)
                    self.assertFalse(app._market_sync_busy)
            finally:
                release.set()
            self.assertEqual(errors, [])

    def test_read_snapshot_does_not_wait_for_remote_sync(self):
        with layout.isolated_app() as (root, app, errors):
            market = Mock()
            market.needs_remote_sync.return_value = True
            market.catalog.return_value = []
            market.summary.return_value = dict(enabled=0, installed=0, total=0, region="test")
            market.last_sync_info.return_value = {}
            with patch.object(app, "_market_obj", return_value=market), \
                 patch.object(app, "_plugin_runtime_obj", return_value=None), \
                 patch.object(app, "_gateway_tool_names", return_value=([], "")):
                data = app._read_market_snapshot()
                market.sync_sources.assert_not_called()
                self.assertTrue(data[4]["needs_sync"])
            self.assertEqual(errors, [])

    def test_pending_render_cannot_continue_after_return_to_chat(self):
        with layout.isolated_app() as (root, app, errors):
            app._show_view("tools")
            app._build_market_panel(app.market_holder)
            app._market_built = True
            app._tools_tab = "market"
            with patch.object(app, "_market_plugin_card") as card:
                app._render_market(snapshot())
                app._return_to_chat()
                calls = card.call_count
                layout.pump(root)
                self.assertEqual(card.call_count, calls)
            self.assertEqual(errors, [])


class ImeEventTests(unittest.TestCase):
    def test_actual_stream_scroll_reaches_end_and_manual_wheel_cancels_pending_scroll(self):
        with layout.isolated_app() as (root, app, errors):
            area = app.chat_area
            message = area.add_agent()
            message.stream_text("line\n" * 120)
            for _ in range(20):
                area.scroll.scroll_to_end()
            layout.pump(root, .3)
            self.assertTrue(area.scroll.at_bottom())
            area.scroll.scroll_to_end()
            area.scroll._on_wheel(Mock(delta=120))
            self.assertIsNone(area.scroll._scroll_job)
            self.assertEqual(errors, [])

    def test_stream_autoscroll_does_not_recursively_flush_global_layout(self):
        scroll = Mock()
        scroll._scroll_job = None
        scroll.after.return_value = "pending"
        for _ in range(30):
            cw.ScrollArea.scroll_to_end(scroll)
        scroll.update_idletasks.assert_not_called()
        scroll.after.assert_called_once_with(16, scroll._scroll_after_layout)

    def test_configure_and_navigation_coalesce_without_nested_event_loop(self):
        anchor = cw._IMECaretAnchor.__new__(cw._IMECaretAnchor)
        anchor._supported = True
        anchor._w = Mock()
        anchor._anchor_job = None
        anchor._w.after_idle.return_value = "pending"
        anchor._update = Mock()
        anchor._anchor_caret = Mock()
        for _ in range(30):
            anchor._on_canvas_or_text_configure()
        anchor._reanchor()
        anchor._w.update_idletasks.assert_not_called()
        anchor._w.after_idle.assert_called_once()
        callback = anchor._w.after_idle.call_args.args[0]
        callback()
        anchor._update.assert_called_once()
        anchor._anchor_caret.assert_called_once()


class DesktopPreferenceConcurrencyTests(unittest.TestCase):
    def test_save_does_not_replace_preferences_while_background_reader_has_them_open(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktop.json"
            path.write_text('{"language":"zh-CN"}', encoding="utf-8")
            entered, release = threading.Event(), threading.Event()
            saved = []
            original_read, original_replace = Path.read_text, gui.os.replace
            def read(target, *args, **kwargs):
                if threading.current_thread().name == "preference-reader":
                    entered.set()
                    release.wait(2)
                return original_read(target, *args, **kwargs)
            def replace(source, target):
                if entered.is_set() and not release.is_set():
                    raise PermissionError("Windows reader denies delete sharing")
                return original_replace(source, target)
            with patch.object(gui, "_desktop_config_path", return_value=path), \
                 patch.object(Path, "read_text", read), patch.object(gui.os, "replace", replace):
                reader = threading.Thread(target=gui.load_desktop_config, name="preference-reader")
                writer = threading.Thread(target=lambda: saved.append(gui.save_desktop_config(language="en")))
                reader.start()
                self.assertTrue(entered.wait(1))
                writer.start()
                try:
                    time.sleep(.05)
                    self.assertEqual(saved, [], "writer must wait until the local reader closes")
                finally:
                    release.set()
                    reader.join(2)
                    writer.join(2)
                self.assertEqual(saved, [True])
                self.assertEqual(gui.load_desktop_config()["language"], "en")


if __name__ == "__main__":
    unittest.main()
