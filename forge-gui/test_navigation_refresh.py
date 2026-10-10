"""Regression coverage for the compact navigation and model-first composer."""
import unittest
from unittest.mock import patch

import forge_gui_v2 as gui
import gui_theme as theme
import i18n
from test_layout_dpi import isolated_app, pump, SCALES


class NavigationRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def test_task_sidebar_manual_open_survives_resize_and_keeps_navigation(self):
        with isolated_app(1, gui.MIN_SIZE) as (root, app, errors):
            app._show_view('task')
            pump(root)
            app.sidebar_reveal_btn.invoke()
            pump(root)
            app._apply_responsive_layout()
            pump(root)
            self.assertTrue(app.sidebar.winfo_ismapped())
            self.assertTrue(app._sidebar_panels['chat'].winfo_ismapped())
            self.assertEqual(app.sidebar.master.pack_slaves()[0], app.sidebar)
            self.assertFalse(errors, errors)

    def test_sidebar_shortcuts_and_composer_actions_are_reachable(self):
        with isolated_app() as (root, app, errors):
            self.assertEqual(tuple(app.sidebar_shortcuts), ('chat', 'tools', 'schedules'))
            self.assertEqual(tuple(app.sidebar_navigation), ('connectors', 'knowledge', 'evolution', 'config'))
            buttons = list(app.sidebar_shortcuts.values())
            self.assertEqual(len({b.winfo_y() for b in buttons}), 1)
            self.assertEqual(sorted(b.winfo_x() for b in buttons), [b.winfo_x() for b in buttons])
            with patch.object(app.desktop_features, 'open') as opened:
                app.sidebar_shortcuts['schedules'].invoke()
                opened.assert_called_once_with(0)
                opened.reset_mock()
                app._nav_click('connectors')
                opened.assert_called_once_with(1)
            app.send_var.set('draft must survive')
            app.input_card.team_settings_btn.invoke()
            self.assertEqual(app._active_view, 'agents')
            app._return_to_chat()
            pump(root)
            self.assertEqual(app.send_var.get(), 'draft must survive')
            self.assertTrue(app.clear_chat_btn.winfo_ismapped())
            self.assertTrue(app.input_card.project_btn.winfo_ismapped())
            self.assertEqual(app.input_card.planning_pill.cget('highlightbackground'), theme.C['warn'])
            self.assertNotIn('default', app.model_combo.cget('values'))
            self.assertFalse(errors, errors)

    def test_picker_reserves_space_for_real_models_at_all_dpi_scales(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, gui.MIN_SIZE) as (root, app, errors):
                picker = app.model_combo
                picker._router_lookup = lambda: {'routing': {'strategy': 'medium'}}
                picker.open_menu()
                pump(root)
                canvas = picker._list_canvas
                self.assertGreaterEqual(canvas.winfo_height(), picker._popup.winfo_height() * .45)
                self.assertTrue(picker._rows)
                first = picker._rows[0][1]
                self.assertLessEqual(first.winfo_rooty()+first.winfo_height(),
                                     canvas.winfo_rooty()+canvas.winfo_height())
                # Advanced controls remain available in the same scrolling surface.
                self.assertEqual(picker._thinking_slider.master.master, picker._list_inner)
                canvas.yview_moveto(1)
                pump(root)
                self.assertLess(picker._thinking_slider.winfo_rooty(),
                                canvas.winfo_rooty()+canvas.winfo_height())
                picker.close_menu()
                self.assertFalse(errors, errors)

    def test_project_switch_from_composer_without_opening_desktop_dialog(self):
        with isolated_app() as (root, app, errors):
            target = app.home / 'new-project'
            target.mkdir()
            self.assertIsNone(app.desktop_features.window)
            app.desktop_features._set_project(target)
            pump(root)
            self.assertEqual(app._active_workspace(), target.resolve())
            self.assertIsNone(app.desktop_features.window)
            self.assertFalse(errors, errors)

    def test_narrow_chat_forced_sidebar_survives_tools_round_trip(self):
        with isolated_app(1, gui.MIN_SIZE) as (root, app, errors):
            app.sidebar_reveal_btn.invoke()
            pump(root)
            self.assertTrue(app._sidebar_visible)
            before = tuple(app.sidebar.master.pack_slaves())
            app._show_view('tools')
            app._return_to_chat()
            pump(root)
            self.assertTrue(app._sidebar_visible)
            self.assertEqual(tuple(app.sidebar.master.pack_slaves()), before)
            self.assertFalse(errors, errors)

    def test_picker_search_does_not_accumulate_thinking_traces(self):
        with isolated_app() as (root, app, errors):
            picker = app.model_combo
            original = len(app.reasoning_var.trace_info())
            picker.open_menu()
            pump(root)
            opened = len(app.reasoning_var.trace_info())
            for query in ('mimo', 'no match', '') * 3:
                picker._search_var.set(query)
                pump(root, .025)
                self.assertEqual(len(app.reasoning_var.trace_info()), opened)
            picker.close_menu()
            self.assertEqual(len(app.reasoning_var.trace_info()), original)
            self.assertFalse(errors, errors)

    def test_localized_composer_actions_stay_inside_at_all_scales(self):
        for scale in SCALES:
            with self.subTest(scale=scale), isolated_app(scale, (1600, 950)) as (root, app, errors):
                for language in i18n.LANGUAGES:
                    with self.subTest(language=language):
                        app._change_language(language)
                        pump(root, .06)
                        for button in (*app.sidebar_shortcuts.values(), app.input_card.project_btn,
                                       app.input_card.project_files_btn, app.input_card.planning_pill,
                                       app.input_card.team_settings_btn):
                            parent = button.master
                            self.assertTrue(button.winfo_ismapped(), str(button))
                            self.assertGreaterEqual(button.winfo_x(), 0)
                            self.assertLessEqual(button.winfo_x()+button.winfo_width(), parent.winfo_width())
                            self.assertLessEqual(button.winfo_y()+button.winfo_height(), parent.winfo_height())
                        self.assertGreater(app.chat_area.winfo_height(), 0)
                self.assertFalse(errors, errors)

    def test_empty_model_catalog_still_offers_api_configuration(self):
        with isolated_app() as (root, app, errors):
            picker = app.model_combo
            with patch.object(picker, '_on_settings') as settings:
                picker.set_values([])
                picker.open_menu()
                pump(root)
                self.assertIsNotNone(picker._popup)
                picker._settings_link.event_generate('<Button-1>')
                pump(root)
                settings.assert_called_once_with(None)
                self.assertIsNone(picker._popup)
                self.assertFalse(errors, errors)
