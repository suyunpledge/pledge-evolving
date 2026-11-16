"""Interaction intent: message-bound retry and explicit scroll follow."""
import tkinter as tk
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import chat_widgets as cw
import forge_gui_v2 as gui
from forge_client import ChatMessage, CompletionResult, GenerationCancelled
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


class TranscriptTests(unittest.TestCase):
    def wait_for(self, root, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            pump(root, .02)
        self.assertTrue(predicate())

    def test_file_snapshot_stays_out_of_bubble_and_display_metadata_stays_off_wire(self):
        with isolated_app() as (root, app, errors):
            payloads, saved = [], []

            class Client:
                def health(self, **kwargs): return True, "ok"
                def stream_chat(self, messages, **kwargs):
                    payloads.extend(message.to_dict() for message in messages)
                    return CompletionResult("Answer without deltas")

            app.client = Client()
            app.send_var.set("Review this file")
            app._attachments = [{"path": "notes.txt", "content": "original snapshot text"}]
            with patch.object(app, "_write_sessions", side_effect=saved.extend):
                app._do_send()
                self.wait_for(root, lambda: not app._sending)
            self.assertEqual(app._agent_msg._text_source, "Answer without deltas")
            self.assertIn("original snapshot text", payloads[-1]["content"])
            self.assertNotIn("display", payloads[-1])
            self.assertEqual(saved[0]["messages"][0]["display"], {
                "text": "Review this file", "attachments": ["notes.txt"]})

            def inspect_bubble():
                bubble = next(widget for widget in app.chat_area.scroll.inner.winfo_children()
                              if isinstance(widget, cw.UserMessage))
                self.assertEqual(bubble.label.cget("text"), "Review this file")
                bubble.attachment_button.invoke()
                dialog = next(widget for widget in root.winfo_children() if isinstance(widget, tk.Toplevel))
                preview = next(widget for widget in dialog.winfo_children() if isinstance(widget, tk.Frame)
                               and any(isinstance(child, tk.Text) for child in widget.winfo_children()))
                editor = next(widget for widget in preview.winfo_children() if isinstance(widget, tk.Text))
                self.assertIn("original snapshot text", editor.get("1.0", "end-1c"))
                self.assertEqual(editor.cget("state"), "disabled")
                dialog.destroy()

            inspect_bubble()
            with patch.object(app, "_load_sessions", return_value=saved), \
                 patch.object(app, "_archive_current_session"):
                app._session_id = "another-session"
                app._load_session(saved[0]["id"])
                self.wait_for(root, lambda: any(isinstance(widget, cw.UserMessage)
                                              for widget in app.chat_area.scroll.inner.winfo_children()))
                inspect_bubble()
            self.assertEqual(errors, [])

    def test_stopping_after_partial_output_keeps_next_draft_and_original_attachment(self):
        with isolated_app() as (root, app, errors):
            class Client:
                def health(self, **kwargs): return True, "ok"
                def stream_chat(self, messages, *, on_chunk, cancel_event, **kwargs):
                    on_chunk("**Partial reply** 😀")
                    if cancel_event.wait(5): raise GenerationCancelled("stopped")
                    raise AssertionError("test generation was not cancelled")

            app.client = Client()
            attachment = {"path": "notes.txt", "content": "original snapshot"}
            app._attachments = [attachment]
            app.send_var.set("original question")
            app._do_send()
            self.wait_for(root, lambda: app._agent_msg._text_source == "**Partial reply** 😀")
            self.assertEqual(app._attachments, [])
            app.send_var.set("next draft")
            app._stop_send()
            self.wait_for(root, lambda: not app._sending)
            self.assertEqual(app.send_var.get(), "next draft")
            self.assertEqual(app._chat_history, [])
            copy, retry = app._agent_msg._action_row.winfo_children()
            copy.invoke()
            self.assertEqual(root.clipboard_get(), "**Partial reply** 😀")
            retry.invoke()
            self.assertEqual(app.send_var.get(), "next draft")
            app.send_var.set("")
            retry.invoke()
            self.assertEqual(app.send_var.get(), "original question")
            self.assertEqual(app._attachments, [attachment])
            self.assertEqual(errors, [])

    def test_failed_turn_restores_draft_and_retry_button_respects_new_edits(self):
        with isolated_app() as (root, app, errors):
            class OfflineClient:
                def health(self, **kwargs): return False, "test gateway offline"

            app.client = OfflineClient()
            attachment = {"path": "original.txt", "content": "original snapshot"}
            app._attachments = [attachment]
            app.send_var.set("original question")
            app._do_send()
            self.wait_for(root, lambda: not app._sending)
            self.assertEqual(app.send_var.get(), "original question")
            self.assertEqual(app._attachments, [attachment])
            notice = next(widget for widget in app.chat_area.scroll.inner.winfo_children()
                          if isinstance(widget, cw.NoticeMessage))
            retry = notice.actions._buttons[-1]
            app.send_var.set("new draft")
            retry.invoke()
            self.assertEqual(app.send_var.get(), "new draft")
            self.assertFalse(app._sending)
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
