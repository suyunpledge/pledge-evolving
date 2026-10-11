"""Workbench contracts using real Tk widgets and isolated application state."""
import tkinter as tk
import unittest

import chat_widgets as cw
import gui_theme as theme
import forge_gui_v2 as gui
import sub_agent as team
import ui_icons
import workspace
from test_layout_dpi import pump, isolated_app, descendants


class DesignSystemTests(unittest.TestCase):
    def test_workspace_destroy_cancels_highlight_only_its_own_timer(self):
        root = tk.Tk()
        root.withdraw()
        try:
            panel = workspace.WorkspacePanel(root, repo_root=gui.HERE)
            panel._scroll_to_line(1)
            token = panel._highlight_after_id
            self.assertIsNotNone(token)
            child = tk.Frame(panel)
            panel._flash_band(child)
            flashes = tuple(panel._transient_after_ids)
            self.assertTrue(flashes)
            other = root.after(15000, lambda: None)
            panel.destroy()
            remaining = root.tk.call('after','info')
            self.assertNotIn(token, remaining)
            for flash in flashes:
                self.assertNotIn(flash, remaining)
            self.assertIn(other, remaining)
            root.after_cancel(other)
        finally:
            root.destroy()

    def test_image_preview_uses_owning_tk_interpreter(self):
        first = tk.Tk()
        first.withdraw()
        second = tk.Tk()
        second.withdraw()
        try:
            panel = workspace.WorkspacePanel(second, repo_root=gui.HERE)
            self.assertTrue(panel._show_image_preview(gui.HERE / 'assets/ui/icons-text-20.png'))
            image = panel._preview_canvas.image
            self.assertIs(image.tk, second.tk)
            self.assertTrue(panel._preview_canvas.find_all())
        finally:
            second.destroy()
            first.destroy()

    def test_code_and_table_copy_preserve_source_and_scroll_locally(self):
        root = tk.Tk()
        root.attributes('-alpha',0)
        try:
            root.geometry('420x600')
            source = '| Field | Value |\n| --- | --- |\n| 模型 | hello |\n| path | ' + 'long/path/'*80 + ' |'
            blocks = cw.parse_blocks(source)
            self.assertEqual(blocks[0]['type'],'table')
            host = cw.render_blocks(root, source+'\n\n```python\nprint(1)\n```')
            host.pack(fill='both',expand=True)
            pump(root)
            cards = [w for w in descendants(host) if isinstance(w, theme.RoundedCard)]
            self.assertEqual(len(cards),2)
            for card, original in zip(cards, (source,'print(1)')):
                card._copy_button.invoke()
                self.assertEqual(root.clipboard_get(), original)
                self.assertLessEqual(card.winfo_width(),host.winfo_width())
                editor = next(w for w in descendants(card) if isinstance(w,tk.Text))
                self.assertTrue(editor.cget('xscrollcommand'))
        finally:
            root.destroy()

    def test_canvas_column_shrinks_and_centers_without_reparenting(self):
        root = tk.Tk()
        root.attributes('-alpha', 0)
        try:
            root.tk.call('tk', 'scaling', 96 / 72)
            canvas = tk.Canvas(root, highlightthickness=0)
            canvas.pack(fill='both', expand=True)
            page = tk.Frame(canvas)
            item = canvas.create_window(0, 0, window=page, anchor='nw')
            theme.bind_canvas_column(canvas, item, max_width=800, gutter=16)
            for width in (1600, 480, 1000):
                root.geometry(f'{width}x400')
                pump(root)
                expected = min(theme.ui_px(root, 800), canvas.winfo_width()-theme.ui_px(root, 32))
                self.assertEqual(page.winfo_width(), expected)
                self.assertEqual(canvas.coords(item)[0], (canvas.winfo_width()-expected)//2)
                self.assertIs(page.master, canvas)
        finally:
            root.destroy()

    def test_tokens_cover_layout_and_effects(self):
        self.assertEqual(theme.C['msg_agent_bg'], theme.C['chat'])
        self.assertEqual(theme.MOTION_MS, 0)
        self.assertIsNone(theme.SHADOW)
        self.assertGreater(theme.CONTENT_WIDTH, theme.FORM_WIDTH)

    def test_emoji_loading_is_paged_and_keeps_cache_identity(self):
        root = tk.Tk()
        root.withdraw()
        try:
            for size in (20, 24, 32):
                image = ui_icons.emoji_image(root, '😀', size=size)
                self.assertIsNotNone(image)
                self.assertIs(image, ui_icons.emoji_image(root, '😀', size=size))
            atlases = [key[1] for key in root._forge_ui_images if key[0] == 'atlas']
            self.assertTrue(atlases)
            self.assertTrue(all('-page-' in name for name in atlases))
            for name in atlases:
                atlas = root._forge_ui_images[('atlas',name)]
                self.assertLessEqual(atlas.height(), 8 * (atlas.width() // 32))
        finally:
            root.destroy()


class WorkbenchTests(unittest.TestCase):
    def test_document_reply_composer_alignment_and_single_error(self):
        with isolated_app(1, (1920, 1080)) as (root, app, errors):
            app.chat_area.clear()
            reply = app.chat_area.add_agent(app=app)
            reply.render_markdown('# Review 😀\n\nRead the code. 🎉')
            pump(root)
            self.assertEqual(reply._bubble._fill, theme.C['chat'])
            self.assertEqual(reply._bubble._outline, '')
            self.assertLessEqual(app.input_card.winfo_width(), theme.ui_px(root, theme.CONTENT_WIDTH))
            self.assertLessEqual(abs(app.input_card.winfo_rootx()-app.chat_area.scroll.inner.winfo_rootx()), theme.ui_px(root, 8))
            app._agent_msg = reply
            app._abort_requested = False
            app._chat_failed('HTTP 402: balance insufficient {"error":"billing"}', 'draft')
            pump(root)
            notices = [w for w in descendants(app.chat_area) if isinstance(w, cw.NoticeMessage)]
            self.assertEqual(len(notices), 1)
            self.assertFalse(any(isinstance(w, tk.Label) and 'balance insufficient' in str(w.cget('text'))
                                 for w in descendants(reply)))
            notice = notices[0]
            self.assertFalse(notice.details.winfo_ismapped())
            notice.toggle_details()
            pump(root)
            self.assertIn('billing', notice.details.display_text())
            self.assertTrue(notice.details.winfo_ismapped())
            self.assertEqual(app.send_var.get(), 'draft')
            self.assertFalse(errors, errors)

    def test_agent_multiline_prompt_save_restore_and_bounded_form(self):
        with isolated_app(1, (1920, 1080)) as (root, app, errors):
            app._show_view('agents')
            app._team_add_preset({'id':'ux-test', 'role':'reviewer', 'system_prompt':'original', 'enabled':True})
            row = app._team_preset_rows[-1]
            prompt = row['prompt_editor']
            self.assertIsInstance(prompt, tk.Text)
            prompt.delete('1.0', 'end')
            prompt.insert('1.0', 'First line\nSecond line 😀')
            pump(root)
            app._team_save()
            config = team.load_config()
            saved = next(p for p in config['sub_agents']['presets'] if p['id']=='ux-test')
            self.assertEqual(saved['system_prompt'], 'First line\nSecond line 😀')
            self.assertEqual(saved['role'], 'reviewer')
            self.assertFalse(app._team_dirty)
            self.assertLessEqual(app._agents_panel_body.winfo_width(), theme.ui_px(root, theme.FORM_WIDTH))
            row['prompt_var'].set('Restored\nvalue')
            pump(root)
            self.assertEqual(prompt.get('1.0','end-1c'), 'Restored\nvalue')
            self.assertFalse(errors, errors)

    def test_sidebar_and_workspace_width_survive_round_trip(self):
        with isolated_app(1, (1920, 1080)) as (root, app, errors):
            app._open_workspace()
            pump(root)
            app.split.sash_place(0, 780, 0)
            pump(root)
            before = app.split.sash_coord(0)[0]
            sidebar_parent = app.sidebar.master
            app._close_workspace()
            app._show_view('tools')
            app._show_view('chat')
            app._open_workspace()
            pump(root)
            self.assertIs(app.sidebar.master, sidebar_parent)
            self.assertLessEqual(abs(app.split.sash_coord(0)[0]-before), 4)
            self.assertFalse(errors, errors)


if __name__ == '__main__':
    unittest.main()
