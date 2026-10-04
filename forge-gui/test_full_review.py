"""Failure-path regressions; local sockets and temporary state only."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import socket
import tempfile
import sys
import traceback
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import chat_widgets as cw
import forge_gui_v2 as gui
import secret_store
import sub_agent as team
import test_runtime_review as runtime
from forge_client import ChatMessage, ForgeGatewayClient, GatewayError, GenerationCancelled
from http_transport import open_response, RequestCancelled


@contextmanager
def stalled_server():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(.1)
    started, release = threading.Event(), threading.Event()
    connections = []

    def accept():
        while not release.is_set():
            try:
                conn, _ = listener.accept()
                connections.append(conn)
                started.set()
            except socket.timeout:
                continue
            except OSError:
                return

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], started, connections
    finally:
        release.set()
        listener.close()
        thread.join(1)
        for conn in connections:
            conn.close()


class TransportTests(unittest.TestCase):
    def cancel_call(self, fn, started, event):
        results = []

        def run():
            try:
                results.append(fn())
            except Exception as exc:
                results.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(started.wait(2))
        before = time.monotonic()
        event.set()
        worker.join(2)
        traces = ""
        if worker.is_alive():
            traces = "\n".join("".join(traceback.format_stack(frame)) for frame in sys._current_frames().values())
        self.assertFalse(worker.is_alive(), "cancellation left a blocked request\n" + traces)
        self.assertLess(time.monotonic() - before, 2)
        return results[0]

    def test_health_cancel_before_response_headers(self):
        with stalled_server() as (port, started, _):
            event = threading.Event()
            client = ForgeGatewayClient(f"http://127.0.0.1:{port}", timeout=20)
            result = self.cancel_call(lambda: client.health(cancel_event=event), started, event)
            self.assertIsInstance(result, GenerationCancelled)

    def test_cancel_during_tls_handshake(self):
        with stalled_server() as (port, started, _):
            event = threading.Event()

            def request():
                with open_response(f"https://127.0.0.1:{port}", timeout=20, cancel_event=event) as response:
                    return response.read()

            self.assertIsInstance(self.cancel_call(request, started, event), RequestCancelled)

    def test_subagent_pool_cancel_interrupts_blocked_network(self):
        with stalled_server() as (port, started, _):
            event = threading.Event()
            agents = [team.SubAgent(id=str(i), role="review", system_prompt="review",
                                    user_message="actual request") for i in range(3)]
            provider = {"wire": "openai", "baseURL": f"http://127.0.0.1:{port}/v1", "model": "local", "apiKey": ""}
            result = self.cancel_call(lambda: team.run_sub_agents(agents, {}, default_provider=provider,
                                                                  default_model="local", cancel_event=event), started, event)
            self.assertEqual(len(result), 3)
            self.assertTrue(all("已停止" in item.error for item in result), [item.error for item in result])

    def test_successful_request_with_event_keeps_response_intact(self):
        import test_forge_client as local
        server, port = local.start_mock_server()
        try:
            client = ForgeGatewayClient(f"http://127.0.0.1:{port}")
            self.assertTrue(client.health(cancel_event=threading.Event())[0])
            self.assertEqual(client.stream_chat([ChatMessage("user", "local test")], cancel_event=threading.Event()).text, "流式 OK")
        finally:
            server.shutdown()
            server.server_close()

    def test_sse_error_is_not_reported_as_empty_success(self):
        response = io.BytesIO(b'data: {"error":{"message":"provider failed"}}\n\ndata: [DONE]\n\n')
        with patch("forge_client.open_response", return_value=response):
            with self.assertRaisesRegex(GatewayError, "provider failed"):
                ForgeGatewayClient().stream_chat([])

    def test_subagent_context_does_not_invent_an_assistant_reply(self):
        agent = team.SubAgent(id="a", role="review", system_prompt="review", user_message="real", context_text="actual context")
        with patch.object(team, "provider_chat", return_value="answer") as call:
            team.run_sub_agents([agent], {}, default_provider={"model": "local"}, default_model="local")
        messages = call.call_args.args[2]
        self.assertFalse(any(message["role"] == "assistant" for message in messages))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "secrets.json"
        self.patch = patch.object(secret_store, "SECRETS_FILE", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_malformed_secret_shape_does_not_crash_or_overwrite(self):
        for raw in ('[]', 'null', '{"provider":42}', '{"":"value"}'):
            self.path.write_text(raw, encoding="utf-8")
            self.assertEqual(secret_store.load(), {})
            self.assertEqual(secret_store.env_for(), {})
            with self.assertRaises(OSError):
                secret_store.set_provider("valid", "new")
            self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_concurrent_secret_updates_keep_both_providers(self):
        threads = [threading.Thread(target=secret_store.set_provider, args=(str(i), str(i))) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(secret_store.load(), {str(i): str(i) for i in range(8)})

    def test_replace_failure_preserves_keys_and_cleans_temp(self):
        secret_store.save({"a": "original"})
        with patch.object(secret_store.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                secret_store.set_provider("a", "changed")
        self.assertEqual(secret_store.load(), {"a": "original"})
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_desktop_replace_failure_is_reported_and_preserves_preferences(self):
        path = self.path.parent / "desktop.json"
        with patch.object(gui, "_desktop_config_path", return_value=path):
            self.assertTrue(gui.save_desktop_config(forge_repo="original"))
            with patch.object(gui.os, "replace", side_effect=OSError("disk unavailable")):
                self.assertFalse(gui.save_desktop_config(forge_repo="changed"))
            self.assertEqual(gui.load_desktop_config()["forge_repo"], "original")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class GuiFailureTests(unittest.TestCase):
    setUp = runtime.TkRuntimeTests.setUp
    tearDown = runtime.TkRuntimeTests.tearDown
    make_app = runtime.TkRuntimeTests.make_app

    def test_restored_draft_is_visible_and_edits_update_payload(self):
        card = cw.InputCard(self.root)
        card.send_var.set("restored\nactual draft")
        self.assertEqual(card.entry.get("1.0", "end-1c"), card.send_var.get())
        card.entry.insert("end", " edited")
        self.root.update()
        self.assertEqual(card.send_var.get(), "restored\nactual draft edited")
        card.send_var.set("")
        self.assertEqual(card.entry.get("1.0", "end-1c"), "")

    def test_invalid_history_timestamps_keep_history_loadable(self):
        app = self.make_app()
        sessions = [{"id": str(i), "title": f"actual session {i}", "updated": value} for i, value in
                    enumerate(("broken", None, float("nan"), float("inf"), 1e99))]
        with patch.object(app, "_load_sessions", return_value=sessions):
            app._refresh_history()
            runtime.TkRuntimeTests.pump(self, lambda: not app._background_jobs["history"]["running"])
        self.assertTrue(app.history_box.winfo_children())

    def edit_model(self):
        app = self.make_app()
        app.user_rows = [{"id": "p", "config": {"wire": "openai", "baseURL": "https://example.invalid/v1",
                                                "model": "old", "smallModel": "separate"}}]
        gui.save_user_layer(app.home, app.user_rows)
        app._model_edit_row_id = "p"
        app.model_edit_var.set("new")
        return app

    def test_model_edit_does_not_replace_newer_disk_config(self):
        app = self.edit_model()
        latest = [{"id": "p", "config": {"model": "external", "baseURL": "https://example.invalid/v1"}}]
        gui.save_user_layer(app.home, latest)
        app._apply_model_edit()
        self.assertEqual(app.user_rows[0]["config"]["model"], "old")
        self.assertEqual(gui.load_user_layer(app.home), latest)

    def test_model_edit_keeps_separate_small_model(self):
        app = self.edit_model()
        with patch.object(app, "_refresh_provider_list"), patch.object(app, "_sync_model_editor"):
            app._apply_model_edit()
        self.assertEqual(app.user_rows[0]["config"]["model"], "new")
        self.assertEqual(app.user_rows[0]["config"]["smallModel"], "separate")

    def test_model_edit_save_error_keeps_memory_and_disk_unchanged(self):
        app = self.edit_model()
        with patch.object(gui, "save_user_layer", side_effect=OSError("disk unavailable")):
            app._apply_model_edit()
        self.assertEqual(app.user_rows[0]["config"]["model"], "old")
        self.assertEqual(gui.load_user_layer(app.home), app.user_rows)

    def test_invalid_favorite_shape_does_not_crash_startup(self):
        with patch.object(gui, "load_desktop_config", return_value={"model_favorites": None}):
            app = self.make_app()
        self.assertEqual(app._model_favorites, [])


if __name__ == "__main__":
    unittest.main()
