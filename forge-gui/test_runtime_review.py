"""Runtime review: real Tk responsiveness, latest-result ordering and shutdown."""
import json
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
import sub_agent as team
import workspace as ws
from forge_client import ForgeGatewayClient, ChatMessage, GatewayError, GenerationCancelled


class TkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if hasattr(self, "app"):
            self.app._closing = True
        try:
            for token in self.root.tk.call("after", "info"):
                self.root.after_cancel(token)
            self.root.destroy()
        except tk.TclError:
            pass
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def pump(self, condition, timeout=3):
        end = time.monotonic() + timeout
        while not condition() and time.monotonic() < end:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(condition())

    def panel(self):
        panel = ws.WorkspacePanel(self.root, repo_root=self.home)
        panel._app = Mock()
        return panel

    def make_app(self):
        with patch.object(gui, "DEFAULT_FORGE_HOME", self.home), \
             patch.object(gui, "_find_run_py", return_value=None), \
             patch.object(gui.ForgeGuiApp, "_repo_root", return_value=self.home), \
             patch.object(gui.ForgeGuiApp, "_start_sysmon"), \
             patch.object(gui, "_autostart_enabled", return_value=False), \
             patch.object(team, "config_path", return_value=self.home / "team.json"):
            self.app = gui.ForgeGuiApp(self.root)
        return self.app

    def test_slow_refresh_keeps_tk_alive_and_latest_request_wins(self):
        panel = self.panel()
        started, calls = threading.Event(), []

        def scan(repo, expanded):
            calls.append(repo)
            if len(calls) == 1:
                started.set()
                self.release.wait(3)
            marker = "old" if len(calls) == 1 else "fresh"
            return False, {marker: "?"}, {}, [{"path": repo, "name": repo.name, "depth": 0, "is_dir": True}]

        with patch.object(panel, "_read_snapshot", side_effect=scan):
            begin = time.monotonic()
            panel.refresh_async()
            self.assertLess(time.monotonic() - begin, .15)
            self.assertTrue(started.wait(1))
            for _ in range(20):
                panel.refresh_async()
            heartbeat = []
            self.root.after(10, lambda: heartbeat.append(True))
            self.pump(lambda: bool(heartbeat))
            self.assertEqual(len(calls), 1)
            self.release.set()
            self.pump(lambda: "fresh" in panel._git_status)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("old", panel._git_status)

    def test_switching_diff_rejects_a_late_previous_file(self):
        panel = self.panel()
        panel._is_git_repo = True
        started = threading.Event()

        def diff(repo, *, path=None):
            if path.name == "first.py":
                started.set()
                self.release.wait(3)
            return f"diff --git {path.name}\n+{path.name}\n"

        with patch.object(ws, "_git_diff", side_effect=diff):
            panel._render_diff(self.home / "first.py")
            self.assertTrue(started.wait(1))
            panel._render_diff(self.home / "second.py")
            self.release.set()
            self.pump(lambda: "second.py" in panel._diff_text.get("1.0", "end"))
            self.assertNotIn("first.py", panel._diff_text.get("1.0", "end"))

    def test_destroy_cancels_poll_and_ignores_background_result(self):
        panel = self.panel()
        applied = []
        panel._submit_io("test", lambda: self.release.wait(2), applied.append)
        panel.destroy()
        self.assertIsNone(panel._io_poll_id)
        self.release.set()
        self.root.update()
        self.assertEqual(applied, [])

    def test_ui_queue_yields_instead_of_draining_a_slow_backlog(self):
        app = self.make_app()
        done = []
        app._post_ui(lambda: time.sleep(.03))
        for i in range(100):
            app._post_ui(done.append, i)
        app._drain_ui_events()
        self.assertLess(len(done), 100)
        self.pump(lambda: len(done) == 100)
        self.assertEqual(done, list(range(100)))

    def test_add_agent_opens_existing_editor_and_failed_save_is_reported(self):
        app = self.make_app()
        count = len(app._team_preset_rows)
        app._prompt_create_agent()
        self.assertEqual(app._active_view, "agents")
        self.assertEqual(len(app._team_preset_rows), count + 1)
        self.assertTrue(app._team_dirty)
        app._team_preset_rows[-1]["enabled_var"].set(False)
        with patch.object(team, "save_config", side_effect=OSError("disk unavailable")):
            app._team_save()
        self.assertIn("保存失败", app.status_var.get())
        self.assertTrue(app._team_dirty)
        with patch.object(team, "save_config") as save:
            app._team_save()
        self.assertFalse(save.call_args.args[0]["sub_agents"]["presets"][-1]["enabled"])
        self.assertFalse(app._team_dirty)

    def test_busy_port_probe_is_async_and_stop_prevents_launch(self):
        app = self.make_app()
        app.run_py = Path(gui.__file__).resolve().parents[1] / "run.py"
        app.user_rows = [{"id": "test", "config": {"baseURL": "https://example.invalid/v1", "model": "test", "apiKey": "test-only", "wire": "openai"}}]
        app.model_var.set("test")
        entered = threading.Event()

        def cleanup(port):
            entered.set()
            self.release.wait(3)

        with patch.object(gui, "port_in_use", return_value=True), \
             patch.object(app, "_clear_stale_gateway_on_port", side_effect=cleanup), \
             patch.object(gui.subprocess, "Popen") as popen:
            begin = time.monotonic()
            self.assertTrue(app._start_gateway())
            self.assertLess(time.monotonic() - begin, .15)
            self.assertTrue(entered.wait(1))
            app._stop_gateway_from_menu()
            self.release.set()
            self.pump(lambda: not app._gateway_starting)
            popen.assert_not_called()

    def test_deferred_start_failure_preserves_retry_count(self):
        app = self.make_app()
        app._autostart_attempts = 3
        with patch.object(gui, "_autostart_enabled", return_value=True), \
             patch.object(app, "_schedule_autostart_retry") as retry:
            app._finish_deferred_gateway(None, {}, threading.Event(), "occupied", True)
        self.assertEqual(app._autostart_attempts, 3)
        retry.assert_called_once_with("occupied")

    def test_close_does_not_wait_on_monitor_in_tk_thread(self):
        app = self.make_app()
        app._archive_current_session = Mock()
        entered = threading.Event()

        def stop():
            entered.set()
            self.release.wait(3)

        app._sysmon = Mock(stop=stop)
        begin = time.monotonic()
        app._on_close()
        self.assertLess(time.monotonic() - begin, .15)
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.root.state(), "withdrawn")
        self.release.set()


class ProcessOwnershipTests(unittest.TestCase):
    def test_stale_cleanup_only_kills_matching_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app = gui.ForgeGuiApp.__new__(gui.ForgeGuiApp)
            app.run_py, app.home = home / "run.py", home
            app._post_ui = Mock()
            command = f'python.exe "{app.run_py}" gateway --home "{home}"'
            for parent_alive, run_path, expected in ((True, command, 2), (False, command.replace("run.py", "other.py"), 2), (False, command, 3)):
                responses = [Mock(stdout="9999"), Mock(stdout=json.dumps({"CommandLine": run_path, "ParentAlive": parent_alive})), Mock(returncode=0)]
                with patch.object(gui, "IS_WINDOWS", True), patch.object(gui, "port_in_use", return_value=True), \
                     patch.object(gui.subprocess, "run", side_effect=responses) as run:
                    app._clear_stale_gateway_on_port_inner(8799)
                    self.assertEqual(run.call_count, expected)
                    if expected == 3:
                        self.assertEqual(run.call_args.args[0][0], "taskkill")

    def test_invalid_team_config_is_repaired_and_failed_write_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "team.json"
            with patch.object(team, "config_path", return_value=path):
                path.write_text(json.dumps({"cluster": {"count": "bad", "lanes": None}, "sub_agents": None, "templates": None}), encoding="utf-8")
                cfg = team.load_config()
                self.assertEqual(cfg["cluster"]["count"], 2)
                self.assertEqual(cfg["cluster"]["lanes"], [])
                before = path.read_bytes()
                with patch.object(team._os, "replace", side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        team.save_config(cfg)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(list(path.parent.glob(".agent-cluster-*.tmp")))


class StreamRuntimeTests(unittest.TestCase):
    def test_disconnect_without_completion_is_not_saved_as_success(self):
        client = ForgeGatewayClient()
        body = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        with patch("forge_client.urllib.request.urlopen", return_value=io.BytesIO(body)):
            with self.assertRaises(GatewayError):
                client.stream_chat([ChatMessage("user", "test")])

    def test_cancel_wakes_a_real_stalled_socket_read(self):
        ready, release, cancel = threading.Event(), threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: ")  # deliberately no newline
                self.wfile.flush()
                ready.set()
                release.wait(3)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        client = ForgeGatewayClient(f"http://127.0.0.1:{server.server_port}", timeout=5)
        errors = []

        def request():
            try:
                client.stream_chat([ChatMessage("user", "local transport test")], cancel_event=cancel)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        try:
            self.assertTrue(ready.wait(2))
            begin = time.monotonic()
            cancel.set()
            worker.join(1)
            self.assertFalse(worker.is_alive(), "cancel waited for the 5-second read timeout")
            self.assertLess(time.monotonic() - begin, 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], GenerationCancelled)
        finally:
            cancel.set()
            release.set()
            worker.join(6)
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
