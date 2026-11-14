"""Offline regressions for icon rendering and exact emoji/copy semantics."""
import tkinter as tk
import unittest

import chat_widgets as cw
import gui_theme as theme
from ui_icons import ICONS, IconCanvas, IconButton, emoji_image, emoji_parts, icon_image


class IconRenderingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("720x400")
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def test_ui_icons_draw_shapes_without_font_glyphs(self):
        for name in ICONS:
            with self.subTest(icon=name):
                icon = IconCanvas(self.root, name)
                self.assertTrue(icon.find_all())
                self.assertNotIn("text", [icon.type(item) for item in icon.find_all()])
                icon.configure(fg="#7C7CF0", bg="#232323")
                icon.destroy()

    def test_native_icon_button_keeps_click_and_dynamic_caption(self):
        called = []
        button = theme.glyph_button(self.root, "⚙", lambda: called.append(1))
        self.assertTrue(button.cget("image"))
        button.invoke()
        self.assertEqual(called, [1])
        button.configure(text="⟳ 重试")
        self.assertEqual(button.cget("text"), "重试")
        self.assertTrue(button.cget("image"))
        button.configure(text="等待中")
        self.assertEqual(button.cget("text"), "等待中")
        self.assertFalse(button.cget("image"))

    def test_send_stop_icons_keep_exact_color_after_state_change(self):
        button = theme.circle_button(self.root, "↑", lambda: None)
        theme.circle_button_state(button, "primary")
        shapes = button.find_withtag("glyph")
        self.assertTrue(shapes)
        self.assertNotIn("text", [button.type(item) for item in shapes])
        self.assertEqual(button.type(shapes[0]), "image")
        bitmap = icon_image(self.root, "send", size=button._icon_size, fg="#FFFFFF")
        self.assertEqual(button.itemcget(shapes[0], "image"), str(bitmap))
        visible = [bitmap.get(x, y) for y in range(bitmap.height()) for x in range(bitmap.width())
                   if not bitmap.transparency_get(x, y)]
        self.assertTrue(visible)
        self.assertEqual(set(visible), {(255, 255, 255)})

    def test_all_library_icons_preserve_dpi_size_and_semantic_color(self):
        for name in ICONS:
            for size in (14, 16, 20, 24, 28, 32, 48, 64):
                with self.subTest(name=name, size=size):
                    bitmap = icon_image(self.root, name, size=size, fg="#53B7A1")
                    self.assertEqual((bitmap.width(), bitmap.height()), (size, size))
                    self.assertIs(bitmap, icon_image(self.root, name, size=size, fg="#53B7A1"))
                    ink = [(x, y) for y in range(size) for x in range(size)
                           if not bitmap.transparency_get(x, y)]
                    self.assertTrue(ink)
                    self.assertEqual(bitmap.get(*ink[len(ink) // 2]), (83, 183, 161))

    def test_colored_emoji_is_a_bitmap_and_copy_preserves_unicode(self):
        image = emoji_image(self.root, "😀")
        self.assertIsNotNone(image)
        colors = {image.get(x, y) for x in range(image.width()) for y in range(image.height())}
        self.assertGreater(len(colors), 20)
        text = "你好 😀 ✅ 🎉 ❤️\n下一行"
        widget = cw.InlineText(self.root)
        widget.pack(fill=tk.X)
        widget.set_text(text)
        self.root.update()
        self.assertGreaterEqual(len(widget.image_names()), 4)
        self.assertEqual(widget.display_text(), text)
        widget.tag_add("sel", "1.0", "end-1c")
        widget._copy_selection()
        self.assertEqual(self.root.clipboard_get(), text)
        self.assertGreater(widget.winfo_height(), 24)
        heading = cw.render_blocks(self.root, "# 完成 🎉 ⏰").winfo_children()[0]
        self.assertEqual(heading.display_text(), "完成 🎉 ⏰")
        self.assertEqual(len(heading.image_names()), 2)

    def test_inline_code_and_links_stay_literal(self):
        widget = cw.InlineText(self.root)
        widget.set_text("😀", "code")
        self.assertFalse(widget.image_names())
        self.assertEqual(widget.get("1.0", "end-1c"), "😀")

    def test_unknown_cluster_and_text_presentation_are_never_substituted(self):
        original = "a 👩‍💻 👍🏽 🇨🇳 1️⃣ b"
        parts = list(emoji_parts(original))
        self.assertEqual("".join(part for part, _emoji in parts), original)
        self.assertIn(("👩‍💻", True), parts)
        self.assertIn(("👍🏽", True), parts)
        self.assertIn(("🇨🇳", True), parts)
        self.assertIsNone(emoji_image(self.root, "❤️\ufe0e"))
        widget = cw.InlineText(self.root)
        widget.set_text(original)
        self.assertEqual(widget.display_text(), original)

    def test_user_bubble_emoji_survives_wrap_and_stream_updates(self):
        message = cw.UserMessage(self.root, "实际输入 😀 🎉")
        message.pack(fill=tk.X)
        self.root.update()
        self.assertEqual(message.label.cget("text"), "实际输入 😀 🎉")
        self.assertEqual(message.label.display_text(), "实际输入 😀 🎉")
        message.label.configure(wraplength=140)
        self.root.update()
        self.assertTrue(message.label.image_names())
        stream = cw.InlineText(self.root)
        stream.set_text("你好 ")
        stream.append_text("😀")
        stream.append_text(" 完成 ✅")
        self.assertEqual(stream.display_text(), "你好 😀 完成 ✅")

    def test_message_copy_action_preserves_rendered_emoji(self):
        message = cw.AgentMessage(self.root)
        message.pack(fill=tk.X)
        original = "**真实文本** 😀 🎉\n\n```python\nprint('hello')\n```"
        message.render_markdown(original)
        message.add_note("Not part of the answer")
        message._action_row.winfo_children()[0].invoke()
        self.assertEqual(original, self.root.clipboard_get())


class InterpreterLifecycleTests(unittest.TestCase):
    def test_image_cache_is_specific_to_owning_tk_root(self):
        for _ in range(2):
            root = tk.Tk()
            try:
                icon = icon_image(root, "config")
                emoji = emoji_image(root, "😀")
                tk.Label(root, image=icon).pack()
                tk.Label(root, image=emoji).pack()
                root.update()
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
