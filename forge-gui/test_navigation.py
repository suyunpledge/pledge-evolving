"""Navigation remains reachable with hidden sidebars and preserves page state."""
import tkinter as tk
import unittest
from unittest.mock import patch

import forge_gui_v2 as gui
import test_layout_dpi as layout
from test_layout_dpi import isolated_app, pump, SCALES


class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def assert_inside(self, widget, ancestor):
        layout.DpiLayoutTests.assert_inside(self, widget, ancestor)

    @staticmethod
    def main_layout(app):
        """Observe actual Tk ownership/docking, not just application booleans."""
        widgets = (app.sidebar.master, app.sidebar, app.split, app.center, app.ws_holder)
        panes = tuple(map(str, app.split.panes()))
        return {
            "widgets": tuple((id(w), w.winfo_parent(), w.winfo_manager(),
                              tuple(sorted((k, str(v)) for k, v in w.pack_info().items()))
                              if w.winfo_manager() == "pack" else (),
                              (w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height())
                              if w.winfo_ismapped() else None) for w in widgets),
            "body_order": tuple(map(str, app.sidebar.master.pack_slaves())),
            "body_children": tuple(map(str, app.sidebar.master.winfo_children())),
            "panes": panes,
            "pane_options": tuple(tuple((k, str(v[-1])) for k, v in
                                         app.split.paneconfigure(p).items()) for p in panes),
            "sashes": tuple(app.split.sash_coord(i) for i in range(len(panes)-1)),
            "sidebar_state": (app._sidebar_visible, app._sidebar_user_hidden,
                              app._sidebar_auto_hidden, app._sidebar_force_open),
            "workspace_state": (id(app.workspace), app._ws_packed,
                                app._workspace_auto_hidden, app._last_workspace_tab),
            "center_order": tuple(map(str, app.center.pack_slaves())),
        }

    def test_tools_return_restores_exact_initial_component_tree_and_docking(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, (1920, 1000)) as (root, app, errors):
                gui.save_desktop_config(reasoning_effort="medium", model_favorites=["test-model"])
                prefs = gui._desktop_config_path().read_bytes()
                for workspace_open in (False, True):
                    if workspace_open:
                        app._open_workspace()
                        pump(root)
                        # A user-selected sash must also survive the round trip.
                        app.split.sash_place(0, app.split.winfo_width()-540, 0)
                    pump(root)
                    before = self.main_layout(app)
                    self.assertEqual(before["body_order"], (str(app.sidebar), str(app.split)))
                    for exit_action in (app.view_back_btn.invoke, app._return_to_chat):
                        for cycle in range(3):
                            with self.subTest(workspace=workspace_open, exit=exit_action.__name__, cycle=cycle):
                                app._show_view("tools")
                                pump(root)
                                exit_action()
                                pump(root)
                                self.assertEqual(self.main_layout(app), before)
                                self.assertEqual(gui._desktop_config_path().read_bytes(), prefs)
                self.assertFalse(errors, errors)

    def test_other_page_returns_and_manual_sidebar_toggle_preserve_left_dock(self):
        with isolated_app(1, (1600, 950)) as (root, app, errors):
            before = self.main_layout(app)
            for page in ("config", "agents", "task", "knowledge", "evolution", "files"):
                app._show_view(page)
                pump(root)
                app.view_back_btn.invoke()
                pump(root)
                self.assertEqual(self.main_layout(app), before, page)
            app._toggle_sidebar()
            pump(root)
            hidden = self.main_layout(app)
            app._show_view("tools")
            pump(root)
            app.view_back_btn.invoke()
            pump(root)
            self.assertEqual(self.main_layout(app), hidden)
            app._toggle_sidebar()
            pump(root)
            self.assertEqual(self.main_layout(app), before)
            self.assertFalse(errors, errors)

    def test_tools_keyboard_return_and_responsive_resize_keep_original_docking(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, (1600, 950)) as (root, app, errors):
                initial = self.main_layout(app)
                for width, height in (gui.MIN_SIZE, (1360, 850), (1600, 950)):
                    root.geometry(f"{width}x{height}")
                    pump(root)
                    before = self.main_layout(app)
                    for shortcut in ("<Alt-Left>", "<Escape>"):
                        app._show_view("tools")
                        root.focus_force()
                        pump(root)
                        root.event_generate(shortcut)
                        pump(root)
                        self.assertEqual(app._active_view, "chat")
                        self.assertEqual(self.main_layout(app), before, (scale, width, shortcut))
                self.assertEqual(self.main_layout(app), initial)
                self.assertFalse(errors, errors)

    def test_every_page_has_fixed_back_button_at_five_scales(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, (1360, 850)) as (root, app, errors):
                for size in ((1360, 850), gui.MIN_SIZE):
                    root.geometry(f"{size[0]}x{size[1]}")
                    pump(root, .05)
                    for key in ("tools", "task", "agents", "config", "knowledge", "evolution", "files"):
                        with self.subTest(scale=scale, size=size, page=key):
                            app._show_view(key)
                            pump(root, .05)
                            self.assertFalse(app._sidebar_visible)
                            self.assert_inside(app.view_back_btn, app.center)
                            self.assertEqual(app.view_back_btn.cget("text"), "返回对话")
                            self.assertTrue(app.view_back_btn.cget("image"))
                            self.assertLess(app.view_navigation.winfo_rooty(), app._views[key].winfo_rooty())
                            app.view_back_btn.invoke()
                            pump(root, .03)
                            self.assertEqual(app._active_view, "chat")
                            self.assertTrue(app.send_entry.winfo_ismapped())
                            self.assertFalse(app.view_navigation.winfo_ismapped())
                self.assertFalse(errors, errors)

    def test_back_tracks_previous_page_and_home_clears_history(self):
        with isolated_app() as (root, app, errors):
            app._show_view("config")
            app._show_view("tools")
            pump(root)
            self.assertEqual(app.view_back_btn.cget("text"), "返回配置")
            self.assert_inside(app.view_chat_btn, app.center)
            app.view_back_btn.invoke()
            self.assertEqual(app._active_view, "config")
            app.view_back_btn.invoke()
            self.assertEqual(app._active_view, "chat")
            app._show_view("task")
            app._show_view("agents")
            app.view_chat_btn.invoke()
            self.assertEqual(app._active_view, "chat")
            self.assertEqual(app._view_history, [])
            self.assertFalse(errors, errors)

    def test_return_preserves_conversation_draft_and_unsaved_settings(self):
        with isolated_app() as (root, app, errors):
            message = app.chat_area.add_user("临时回归测试消息")
            app._chat_history = [gui.ChatMessage("user", "临时回归测试消息")]
            history = list(app._chat_history)
            app.send_var.set("尚未发送的草稿")
            app._clear_placeholder()
            app.input_text.insert("1.0", "尚未保存的配置草稿")
            feature = app._feature_entries[0]
            feature["var"].set(not feature["value"])
            app._mark_features_dirty()
            expected = feature["var"].get()
            with patch.object(gui.messagebox, "askyesno") as confirmation:
                for key in ("tools", "config", "agents", "task"):
                    app._show_view(key)
                    app.view_back_btn.invoke()
                confirmation.assert_not_called()
            self.assertEqual(app._chat_history, history)
            self.assertTrue(message.winfo_exists())
            self.assertEqual(app.send_var.get(), "尚未发送的草稿")
            self.assertEqual(app.input_text.get("1.0", "end-1c"), "尚未保存的配置草稿")
            self.assertEqual(feature["var"].get(), expected)
            self.assertTrue(app._feature_dirty)
            self.assertFalse(errors, errors)

    def test_scrolling_tools_does_not_hide_return_and_workspace_is_preserved(self):
        with isolated_app(1, (1600, 950)) as (root, app, errors):
            app.user_rows += [{"id": f"test-feature-{n}", "config": {"enabled": True}} for n in range(35)]
            app._rebuild_feature_toggles(force=True)
            app._open_workspace()
            panel = app.workspace
            app._show_view("tools")
            pump(root)
            app.feature_canvas.yview_moveto(1)
            pump(root)
            self.assertGreater(app.feature_canvas.yview()[0], 0)
            self.assert_inside(app.view_back_btn, app.center)
            app.view_back_btn.invoke()
            pump(root)
            self.assertTrue(app._ws_packed)
            self.assertIs(app.workspace, panel)
            app._close_workspace()
            self.assertFalse(app._ws_packed)
            self.assertTrue(app.send_entry.winfo_ismapped())
            self.assertFalse(errors, errors)

    def test_keyboard_escape_and_alt_left_return_without_changing_draft(self):
        with isolated_app() as (root, app, errors):
            app.send_var.set("保留键盘测试草稿")
            app._show_view("config")
            app._show_view("tools")
            root.focus_force()
            pump(root)
            root.event_generate("<Alt-Left>")
            pump(root)
            self.assertEqual(app._active_view, "config")
            root.event_generate("<Escape>")
            pump(root)
            self.assertEqual(app._active_view, "chat")
            self.assertEqual(app.send_var.get(), "保留键盘测试草稿")
            self.assertFalse(errors, errors)

    def test_tools_market_and_feature_tabs_both_keep_return_reachable(self):
        with isolated_app(1.5, gui.MIN_SIZE) as (root, app, errors):
            for key in ("market", "features"):
                app._show_view("tools")
                app._tools_tab_buttons[key].invoke()
                pump(root)
                holder = app.market_holder if key == "market" else app.feature_holder
                self.assertTrue(holder.winfo_ismapped())
                self.assert_inside(app.view_back_btn, app.center)
                app.view_back_btn.invoke()
                pump(root)
                self.assertEqual(app._active_view, "chat")
                app._show_view("tools")
                pump(root)
                self.assertEqual(app._tools_tab, key)
                app.view_back_btn.invoke()
            self.assertFalse(errors, errors)

    def test_file_sidebar_mode_can_return_to_normal_conversation(self):
        with isolated_app(1, (1600, 950)) as (root, app, errors):
            app._nav_click("files")
            pump(root)
            self.assertTrue(app.view_navigation.winfo_ismapped())
            app.view_back_btn.invoke()
            pump(root)
            self.assertEqual(app._active_nav, "chat")
            self.assertTrue(app._sidebar_panels["chat"].winfo_ismapped())
            self.assertFalse(app.view_navigation.winfo_ismapped())
            self.assertFalse(errors, errors)

    def test_context_dialog_has_explicit_close_and_escape(self):
        with isolated_app() as (root, app, errors):
            app._open_context()
            dialog = app.context_close_btn.winfo_toplevel()
            dialog.attributes("-alpha", 0)
            pump(root)
            self.assert_inside(app.context_close_btn, dialog)
            app.context_close_btn.invoke()
            self.assertFalse(dialog.winfo_exists())
            app._open_context()
            dialog = app.context_close_btn.winfo_toplevel()
            dialog.attributes("-alpha", 0)
            dialog.focus_force()
            pump(root)
            dialog.event_generate("<Escape>")
            pump(root)
            self.assertFalse(dialog.winfo_exists())
            self.assertFalse(errors, errors)

    def test_modal_dialog_escape_closes_only_the_dialog_and_releases_grab(self):
        with isolated_app() as (root, app, errors):
            app._show_view("tools")
            for open_dialog in (app._open_api_keys, lambda: app._open_provider_catalog("no-navigation-test-match")):
                open_dialog()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                dialog.attributes("-alpha", 0)
                self.assertIs(root.grab_current(), dialog)
                self.assertIsNone(app._navigate_back())
                self.assertEqual(app._active_view, "tools")
                dialog.focus_force()
                pump(root)
                dialog.event_generate("<Escape>")
                pump(root)
                self.assertFalse(dialog.winfo_exists())
                self.assertIsNone(root.grab_current())
                self.assertEqual(app._active_view, "tools")
            app.view_back_btn.invoke()
            self.assertEqual(app._active_view, "chat")
            self.assertFalse(errors, errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
