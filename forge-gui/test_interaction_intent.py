"""Interaction intent: message-bound retry and explicit scroll follow."""
import tkinter as tk
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import chat_widgets as cw
import forge_gui_v2 as gui
from forge_client import ChatMessage
from test_layout_dpi import isolated_app, pump


class RetryIntentTests(unittest.TestCase):
    def app(self, draft=""):
        app = SimpleNamespace(
            _sending=False, _session_id="current", _attachments=[],
            _chat_history=[ChatMessage("user", "newer unrelated question")],
            send_var=Mock(), input_card=Mock(), _set_status=Mock(),
            _do_send=Mock(), _update_context_summary=Mock())
        app.send_var.get.return_value = draft
        return app

    def message(self, session="current"):
        return SimpleNamespace(_retry_session=session, _retry_prompt="original question",
                               _retry_attachments=(), winfo_exists=lambda: True)

    def test_retry_prepares_clicked_message_without_executing(self):
        app = self.app()
        gui.ForgeGuiApp._retry_last_agent(app, self.message())
        app.send_var.set.assert_called_once_with("original question")
        app._do_send.assert_not_called()
        app.input_card.focus_entry.assert_called_once()

    def test_retry_does_not_overwrite_draft_or_attachments(self):
        for draft, attachments in (("unfinished draft", []), ("", [{"path": "new.txt"}])):
            with self.subTest(draft=draft):
                app = self.app(draft)
                app._attachments = attachments
                gui.ForgeGuiApp._retry_last_agent(app, self.message())
                app.send_var.set.assert_not_called()
                self.assertEqual(app._attachments, attachments)
                app._do_send.assert_not_called()

    def test_stale_session_and_unbound_messages_cannot_retry(self):
        for msg in (self.message("old"), SimpleNamespace(), None):
            app = self.app()
            gui.ForgeGuiApp._retry_last_agent(app, msg)
            app.send_var.set.assert_not_called()
            app._do_send.assert_not_called()

    def test_history_buttons_bind_their_own_preceding_user(self):
        with isolated_app() as (root, app, errors):
            app.chat_area.clear()
            messages = [ChatMessage("user", "first question"), ChatMessage("assistant", "first reply"),
                        ChatMessage("user", "second question"), ChatMessage("assistant", "second reply")]
            app._render_history_messages(messages)
            pump(root, .3)
            cards = [w for w in app.chat_area.scroll.inner.winfo_children() if isinstance(w, cw.AgentMessage)]
            self.assertEqual([getattr(w, "_retry_prompt", None) for w in cards],
                             ["first question", "second question"])
            self.assertTrue(all(w._retry_from_history for w in cards))
            with patch.object(app, "_set_status") as status:
                app._retry_last_agent(cards[0])
                self.assertIn("文字快照", str(status.call_args.args[0]))
            self.assertEqual(app.send_var.get(), "first question")
            self.assertEqual(app._attachments, [])
            self.assertEqual(errors, [])

    def test_completed_turn_keeps_protected_prompt_and_attachment_snapshot_for_retry(self):
        with isolated_app() as (root, app, errors):
            class Client:
                def health(self, **kwargs): return True, "ok"
                def stream_chat(self, messages, **kwargs):
                    kwargs["on_chunk"]("reply")
                    return SimpleNamespace(reasoning_content="")
            app.client = Client()
            app.chat_area.clear()
            tk.Frame(app.chat_area.scroll.inner, height=20000).pack(fill=tk.X)
            pump(root)
            app.chat_area.scroll._manual_scroll("moveto", .4)
            secret = "sk-test-interaction-secret-93874398274398274"
            app.send_var.set("OPENAI_API_KEY=" + secret)
            attachment = {"path": "sample.txt", "content": "original file snapshot"}
            app._attachments = [dict(attachment)]
            app._do_send()
            deadline = time.monotonic() + 3
            while app._sending and time.monotonic() < deadline:
                pump(root, .05)
            self.assertFalse(app._sending)
            self.assertTrue(app.chat_area.scroll.at_bottom(), "Sending is an explicit request to reveal the new turn")
            msg = app._agent_msg
            self.assertEqual(app._attachments, [])
            self.assertNotIn(secret, msg._retry_prompt)
            self.assertIn("{{SECRET_REF:", msg._retry_prompt)
            history = list(app._chat_history)
            with patch.object(app, "_do_send") as send:
                app._retry_last_agent(msg)
                send.assert_not_called()
            self.assertEqual(app._chat_history, history)
            self.assertEqual(app._attachments, [attachment])
            app._attachments[0]["content"] = "edited draft snapshot"
            self.assertEqual(msg._retry_attachments[0]["content"], "original file snapshot")
            self.assertEqual(errors, [])


class ScrollIntentTests(unittest.TestCase):
    def test_long_conversation_scroll_up_does_not_resume_on_delta(self):
        with isolated_app() as (root, app, errors):
            area = app.chat_area
            area.clear()
            tk.Frame(area.scroll.inner, height=20000).pack(fill=tk.X)
            app._agent_msg = area.add_agent(app=app)
            area.scroll.scroll_to_end()
            pump(root)
            area.scroll._manual_scroll("moveto", .93)
            pump(root)
            before = area.scroll.canvas.yview()
            self.assertGreater(before[1], .90)  # Old percentage threshold incorrectly follows.
            app._append_stream_delta("new output")
            pump(root)
            self.assertLess(area.scroll.canvas.yview()[1], .99)
            self.assertTrue(area.jump_latest.winfo_ismapped())
            area.jump_latest.invoke()
            pump(root)
            self.assertTrue(area.scroll.at_bottom())
            self.assertFalse(area.jump_latest.winfo_ismapped())
            app._append_stream_delta("\nmore output")
            pump(root)
            self.assertTrue(area.scroll.at_bottom())
            self.assertEqual(errors, [])

    def test_one_scroll_step_pauses_even_inside_near_bottom_threshold(self):
        root = tk.Tk()
        root.attributes("-alpha", 0)
        try:
            root.geometry("600x400")
            area = cw.ScrollArea(root)
            area.pack(fill=tk.BOTH, expand=True)
            tk.Frame(area.inner, height=20000).pack(fill=tk.X)
            area.scroll_to_end()
            pump(root)
            area._on_wheel(SimpleNamespace(delta=120))
            self.assertFalse(area.near_bottom())
            area._manual_scroll("moveto", 1)
            self.assertTrue(area.near_bottom())
        finally:
            root.destroy()
