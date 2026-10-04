"""Localization contracts: display-only, persistent, no layout reconstruction."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tkinter as tk

import i18n
import forge_gui_v2 as gui
import test_layout_dpi as layout
import test_plugin_market_ui as market_ui


class TranslationTests(unittest.TestCase):
    def test_catalogs_complete_and_placeholders_match(self):
        self.assertEqual(len(i18n.LANGUAGES), 10)
        for code in i18n.LANGUAGES:
            self.assertEqual(i18n.validate_catalog(code), [])

    def test_source_is_stable_and_payload_is_not_translated(self):
        text = i18n.tr("对话")
        self.assertEqual(str(text), "对话")
        self.assertEqual(text.render("en"), "Chat")
        self.assertEqual(i18n.resolve("对话", "en"), "对话")

    def test_safe_fallback_and_formatting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "en.json"
            path.write_text('{"对话": "Chat", "已安装 {count}": "Installed {wrong}"}', encoding="utf-8")
            locale = i18n.Translator("en", directory=Path(tmp))
            self.assertEqual(locale.render(i18n.tr("不存在的文字")), "不存在的文字")
            self.assertEqual(locale.render(i18n.tr("已安装 {count}", count=3)), "已安装 3")
            path.write_text('broken', encoding="utf-8")
            self.assertEqual(i18n.Translator("en", directory=Path(tmp)).render(i18n.tr("对话")), "对话")
        self.assertEqual(i18n.normalize_language("../en"), "zh-CN")
        self.assertEqual(i18n.normalize_language("fr_FR"), "fr")


class LanguageUiTests(unittest.TestCase):
    def test_all_languages_at_five_scales_keep_main_controls_visible(self):
        for scale in layout.SCALES:
            with market_ui.market_app(scale, gui.MIN_SIZE) as (root, app, errors):
                with patch.object(app, "_gateway_tool_names", return_value=([], "offline")):
                    app.market_entry_btn.invoke()
                    layout.pump(root, .2)
                    for code in i18n.LANGUAGES:
                        with self.subTest(scale=scale, language=code):
                            self.assertTrue(app._change_language(code))
                            layout.pump(root, .06)
                            for button in (app.view_back_btn, app.language_picker,
                                           *app._tools_tab_buttons.values()):
                                if button is app.language_picker:
                                    continue  # settings is not the current view
                                self.assertTrue(button.winfo_ismapped())
                                self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth())
                            for widget in layout.descendants(app.market_holder):
                                if isinstance(widget, tk.Button) and widget.winfo_ismapped():
                                    self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(),
                                                            widget.cget("text"))
                                    self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),
                                                         app.market_holder.winfo_rootx()+app.market_holder.winfo_width(),
                                                         widget.cget("text"))
                            app._return_to_chat()
                            layout.pump(root, .04)
                            self.assertGreaterEqual(app.market_entry_btn.winfo_width(), app.market_entry_btn.winfo_reqwidth())
                            for control in (app.input_card.plus, app.input_card.team_pill._btn,
                                            app.input_card.mode_pill, app.input_card.send_circle):
                                self.assertTrue(control.winfo_ismapped())
                                self.assertLessEqual(control.winfo_rootx()+control.winfo_width(),
                                                     app.input_card.winfo_rootx()+app.input_card.winfo_width())
                            app.market_entry_btn.invoke()
                    self.assertFalse(errors)

    def test_switch_keeps_draft_history_and_layout_and_persists(self):
        with layout.isolated_app() as (root, app, errors):
            app.send_var.set("对话 — 用户原文 🧪")
            app._show_view("config")
            app.input_text.insert("end", "\n// unsaved")
            before = (root.winfo_children(), app.split.panes(), app.sidebar.master,
                      app.sidebar.winfo_manager(), app._active_view, app.input_text.get("1.0", "end"))
            self.assertTrue(app._change_language("en"))
            self.assertEqual(app.send_var.get(), "对话 — 用户原文 🧪")
            self.assertEqual(before, (root.winfo_children(), app.split.panes(), app.sidebar.master,
                                      app.sidebar.winfo_manager(), app._active_view, app.input_text.get("1.0", "end")))
            self.assertEqual(gui.load_desktop_config()["language"], "en")
            self.assertEqual(app.market_entry_btn.cget("text"), "Plugin Market")
            self.assertFalse(errors)
            self.assertTrue(app._change_language("ja"))
            self.assertEqual(app.market_entry_btn.cget("text"), "プラグイン市場")

    def test_save_failure_does_not_claim_changed_language(self):
        with layout.isolated_app() as (root, app, errors):
            with patch.object(gui, "save_desktop_config", return_value=False):
                self.assertFalse(app._change_language("en"))
            self.assertEqual(root._forge_locale.language, "zh-CN")
            self.assertIn("保存", app.status_var.get())
            self.assertFalse(errors)

    def test_persisted_language_is_used_before_first_widget(self):
        with patch.object(gui, "load_desktop_config", return_value={"language": "fr"}):
            with layout.isolated_app() as (root, app, errors):
                self.assertEqual(app.market_entry_btn.cget("text"), "Marché des extensions")
                self.assertEqual(app.language_var.get(), "Français")
                self.assertIn("Discussion", root.title())
                self.assertFalse(errors)

    def test_two_interpreters_and_destroyed_bindings(self):
        roots = [tk.Tk(), tk.Tk()]
        try:
            for root in roots:
                root.withdraw()
                root._forge_locale = i18n.Translator()
            first = i18n.Label(roots[0], text=i18n.tr("对话"))
            second = i18n.Label(roots[1], text=i18n.tr("对话"))
            roots[0]._forge_locale.switch("en")
            self.assertEqual(first.cget("text"), "Chat")
            self.assertEqual(second.cget("text"), "对话")
            first.configure(text="对话")
            roots[0]._forge_locale.switch("fr")
            self.assertEqual(first.cget("text"), "对话")
            first.destroy()
            roots[0]._forge_locale.switch("ja")
        finally:
            for root in roots:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
