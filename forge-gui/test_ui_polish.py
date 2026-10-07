"""Real Tk regressions for narrow cards, keyboard disclosure and scoped wheels."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tkinter as tk
from tkinter import font as tkfont
import unittest

import chat_widgets as cw
import forge_gui_v2 as gui
import gui_theme as theme
import i18n
import plugin_market as pm
from test_layout_dpi import descendants, pump, SCALES


class UiPolishTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes("-alpha", 0)
        self.root._forge_locale = i18n.Translator()
        self.root.geometry("500x700+0+0")
        self.root.tk.call("tk", "scaling", 96 / 72)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        owners = [self.root, *descendants(self.root)]
        for token in self.root.tk.call("after", "info"):
            command = self.root.tk.call("after", "info", token)[0]
            owner = next((widget for widget in owners if command in (widget._tclCommands or [])), self.root)
            owner.after_cancel(token)
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def assert_fits(self, widget, ancestor):
        self.assertGreater(widget.winfo_width(), 1, str(widget))
        self.assertGreaterEqual(widget.winfo_rootx(), ancestor.winfo_rootx(), str(widget))
        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                             ancestor.winfo_rootx() + ancestor.winfo_width(), str(widget))
        self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))
        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget))

    def market(self):
        area = cw.ScrollArea(self.root, bg=theme.C["bg"])
        area.pack(fill="both", expand=True)
        return SimpleNamespace(market_list=area.inner, viewport=area, _session_id="ui-test", _repo_root=lambda: Path.cwd(),
                               _market_action=lambda *_: None, _inspect_plugin=lambda *_: None)

    def test_plugin_long_metadata_and_translated_actions_fit_at_five_scales(self):
        plugin = pm.parse_manifest({"id": "ui-fixture", "name": "Long plugin name " * 8,
            "summary": "Long multilingual summary 中文説明 " * 12,
            "author": "Long publisher " * 12, "homepage": "https://example.invalid/" + "path/" * 35,
            "capabilities": ["repo.read", "repo.write", "process.exec", "network.github"]})
        plugin.installed = plugin.enabled = True
        plugin.error = "Manifest validation explanation " * 12
        plugin.runtime_status = "Unavailable because " * 12
        plugin.grants = [{"capabilities": ["repo.read"]}]
        metrics = {}
        for scale in SCALES:
            self.root.tk.call("tk", "scaling", scale * 96 / 72)
            app = self.market()
            gui.ForgeGuiApp._market_plugin_card(app, plugin)
            metrics[scale] = tkfont.Font(root=self.root, font=theme.FONT_UI).metrics("linespace")
            for language in i18n.LANGUAGES:
                with self.subTest(scale=scale, language=language):
                    self.root._forge_locale.switch(language)
                    pump(self.root, .06)
                    for widget in descendants(app.market_list):
                        if isinstance(widget, (tk.Label, tk.Button)):
                            self.assert_fits(widget, app.market_list)
                            if isinstance(widget, tk.Label) and len(widget.cget("text")) > 100:
                                self.assertGreater(int(widget.cget("wraplength")), 0)
                                self.assertLessEqual(int(widget.cget("wraplength")), widget.winfo_width())
            app.viewport.destroy()
        self.assertGreater(metrics[2], metrics[1] * 1.7)

    def test_tool_list_long_names_and_descriptions_stay_readable(self):
        app = self.market()
        tools = [pm.ToolEntry("plugin.namespace." * 10, "plugin", "Long description 中文 " * 25)]
        gui.ForgeGuiApp._market_tool_section(app, tools)
        pump(self.root)
        for widget in descendants(app.market_list):
            if isinstance(widget, tk.Label):
                self.assert_fits(widget, app.market_list)
                if len(widget.cget("text")) > 100:
                    self.assertGreater(int(widget.cget("wraplength")), 0)

    def test_tool_and_step_disclosures_support_return_and_space(self):
        cards = [cw.ToolCard(self.root, rows=[{"name": "read_file"}]),
                 cw.StepList(self.root, [{"title": "Read a file"}])]
        for card in cards:
            card.pack(fill="x")
        pump(self.root)
        for card in cards:
            with self.subTest(card=type(card).__name__):
                self.assertEqual(str(card._summary.cget("takefocus")), "1")
                card._summary.focus_force()
                pump(self.root, .02)
                card._summary.event_generate("<Return>")
                self.assertTrue(card._expanded)
                self.assertEqual(card._arrow.name, "chevron_down")
                card._summary.event_generate("<space>")
                self.assertFalse(card._expanded)
                self.assertEqual(card._arrow.name, "chevron_right")

    def scroll_area(self):
        area = cw.ScrollArea(self.root)
        area.pack(fill="both", expand=True)
        labels = []
        for n in range(60):
            label = tk.Label(area.inner, text=f"Row {n}", height=2)
            label.pack(fill="x")
            labels.append(label)
        pump(self.root)
        return area, labels

    def test_enter_leave_does_not_replace_or_remove_global_wheel_handlers(self):
        self.root.bind_all("<MouseWheel>", lambda _: None, add="+")
        before = self.root.bind_all("<MouseWheel>")
        area, _ = self.scroll_area()
        area.event_generate("<Enter>")
        pump(self.root, .02)
        self.assertEqual(self.root.bind_all("<MouseWheel>"), before)
        area.event_generate("<Leave>")
        self.assertEqual(self.root.bind_all("<MouseWheel>"), before)

    def test_wheel_on_child_scrolls_without_enter_and_nested_text_keeps_its_scroll(self):
        area, labels = self.scroll_area()
        labels[0].event_generate("<MouseWheel>", delta=-120)
        pump(self.root, .02)
        self.assertGreater(area.canvas.yview()[0], 0)
        area.canvas.yview_moveto(0)
        text = tk.Text(area.inner, height=3)
        text.insert("1.0", "nested scroll\n" * 100)
        text.pack(before=labels[0], fill="x")
        pump(self.root)
        text.event_generate("<MouseWheel>", delta=-120)
        pump(self.root, .02)
        self.assertGreater(text.yview()[0], 0)
        self.assertEqual(area.canvas.yview()[0], 0)

    def test_destroy_removes_only_its_own_wheel_callback(self):
        self.root.bind("<MouseWheel>", lambda _: None, add="+")
        before = self.root.bind("<MouseWheel>")
        area, _ = self.scroll_area()
        area.destroy()
        pump(self.root, .02)
        self.assertEqual(self.root.bind("<MouseWheel>").strip(), before.strip())

    def test_sibling_view_and_readonly_inline_text_route_to_their_own_area(self):
        areas, labels = [], []
        for _ in range(2):
            area = cw.ScrollArea(self.root)
            area.pack(side="left", fill="both", expand=True)
            for n in range(40):
                tk.Label(area.inner, text=str(n), height=2).pack(fill="x")
            inline = cw.InlineText(area.inner)
            inline.set_text("Read-only inline content")
            inline.pack(fill="x", before=area.inner.winfo_children()[0])
            areas.append(area)
            labels.append(inline)
        pump(self.root)
        labels[1].event_generate("<MouseWheel>", delta=-120)
        self.assertEqual(areas[0].canvas.yview()[0], 0)
        self.assertGreater(areas[1].canvas.yview()[0], 0)
        areas[1].destroy()
        labels[0].event_generate("<MouseWheel>", delta=-120)
        self.assertGreater(areas[0].canvas.yview()[0], 0)

    def test_trace_long_text_wraps_without_hiding_elapsed_status(self):
        area = cw.ScrollArea(self.root)
        area.pack(fill="both", expand=True)
        widgets = [cw.ToolCard(area.inner, expanded=True, rows=[{
            "name": "long_tool_name_" * 12, "desc": "Detailed output " * 30, "elapsed": "12.6s"}]),
            cw.StepList(area.inner, [{"title": "Long step title " * 20,
                "desc": "Step description " * 30, "elapsed": "12.6s", "done": True}], expanded=True)]
        for widget in widgets:
            widget.pack(fill="x")
        pump(self.root)
        for widget in widgets:
            for child in descendants(widget):
                if isinstance(child, tk.Label):
                    self.assert_fits(child, widget)

    def test_nested_scroll_area_moves_only_the_nearest_viewport(self):
        outer, outer_labels = self.scroll_area()
        inner = cw.ScrollArea(outer.inner, bg=theme.C["surface"])
        inner.configure(height=150)
        inner.pack_propagate(False)
        inner.pack(fill="x", before=outer_labels[0])
        for n in range(30):
            tk.Label(inner.inner, text=str(n), height=2).pack(fill="x")
        pump(self.root)
        inner.inner.winfo_children()[0].event_generate("<MouseWheel>", delta=-120)
        self.assertEqual(outer.canvas.yview()[0], 0)
        self.assertGreater(inner.canvas.yview()[0], 0)

    def test_tool_detail_tooltip_still_opens_over_name_and_description(self):
        card = cw.ToolCard(self.root, expanded=True, rows=[{"name": "read_file",
                           "desc": "README.md", "detail": "100 lines read"}])
        card.pack(fill="x")
        pump(self.root)
        labels = [w for w in descendants(card.rows_frame) if isinstance(w, tk.Label)
                  and w.cget("text") in ("read_file", "README.md")]
        self.assertEqual(len(labels), 2)
        for label in labels:
            label.event_generate("<Enter>")
            pump(self.root, .5)
            self.assertTrue(any(isinstance(w, tk.Toplevel) for w in label.winfo_children()), label.cget("text"))
            label.event_generate("<Leave>")
            pump(self.root, .02)

    def test_destroyed_markdown_host_cancels_pending_render_and_never_finishes(self):
        done = []
        host = cw.render_blocks_chunked(self.root, "\n\n".join(["test paragraph"] * 40),
                                        on_done=lambda _: done.append(True))
        owned_jobs = [token for token in self.root.tk.call("after", "info")
                      if self.root.tk.call("after", "info", token)[0] in (host._tclCommands or [])]
        self.assertTrue(owned_jobs, "the test must interrupt an active render chain")
        host.destroy()
        self.assertTrue(set(owned_jobs).isdisjoint(self.root.tk.call("after", "info")))
        pump(self.root, .02)
        self.assertEqual(done, [])

    def history_app(self):
        app = object.__new__(gui.ForgeGuiApp)
        app.root = self.root
        app.chat_area = cw.MessageArea(self.root)
        app.chat_area.pack(fill="both", expand=True)
        app._closing = False
        app._session_id = "old"
        app._refresh_history = lambda: None
        return app

    def test_switching_history_cannot_append_messages_from_the_previous_session(self):
        app = self.history_app()
        app._render_history_messages([gui.ChatMessage("user", "old fixture")] * 40)
        app._session_id = "new"
        app.chat_area.clear()
        app._render_history_messages([gui.ChatMessage("user", "new fixture")])
        pump(self.root)
        messages = [w.label.cget("text") for w in app.chat_area.scroll.inner.winfo_children()
                    if isinstance(w, cw.UserMessage)]
        self.assertEqual(messages, ["new fixture"])

    def test_destroying_history_area_removes_root_owned_render_jobs(self):
        app = self.history_app()
        before = set(self.root.tk.call("after", "info"))
        app._render_history_messages([gui.ChatMessage("user", "fixture")] * 40)
        jobs = set(self.root.tk.call("after", "info")) - before
        self.assertTrue(jobs)
        app.chat_area.destroy()
        self.assertTrue(jobs.isdisjoint(self.root.tk.call("after", "info")))

    def test_new_session_cancels_an_incomplete_history_load(self):
        import test_plugin_market_ui as fixture
        with fixture.market_app() as (root, app, errors):
            with patch.object(app, "_refresh_history"):
                app._render_history_messages([gui.ChatMessage("user", "test fixture")] * 40)
                app._new_session()
                pump(root)
                self.assertFalse(any(isinstance(w, cw.UserMessage)
                                     for w in app.chat_area.scroll.inner.winfo_children()))
                self.assertIsNone(app._history_render_job)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
