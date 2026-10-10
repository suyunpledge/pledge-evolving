"""Bounded startup, cancellation and stale worker isolation (no network)."""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
from test_layout_dpi import isolated_app, pump


def configure(app):
    app.run_py = Path(gui.__file__).resolve().parents[1] / "run.py"
    app.user_rows = [{"id": "test", "config": {
        "wire": "openai", "baseURL": "https://example.invalid/v1",
        "model": "default", "apiKey": "test-only"}}]
    app.model_var.set("default")


class GatewayStartupLifecycleTests(unittest.TestCase):
    def test_windows_reserved_port_uses_an_os_allocated_loopback_port(self):
        first, second = Mock(), Mock()
        first.__enter__ = Mock(return_value=first)
        first.__exit__ = Mock(return_value=False)
        second.__enter__ = Mock(return_value=second)
        second.__exit__ = Mock(return_value=False)
        denied = PermissionError("Windows reserved port")
        denied.winerror = 10013
        first.bind.side_effect = denied
        second.getsockname.return_value = ("127.0.0.1", 18899)
        with patch.object(gui.socket, "socket", side_effect=[first, second]):
            self.assertEqual(gui.bindable_gateway_port(8799), 18899)
        first.bind.assert_called_once_with(("127.0.0.1", 8799))
        second.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_occupied_port_is_not_silently_switched(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.bind.side_effect = OSError(10048, "occupied")
        with patch.object(gui.socket, "socket", return_value=sock):
            with self.assertRaises(OSError):
                gui.bindable_gateway_port(8799)

    def test_fallback_port_matches_command_ui_and_client_before_probe(self):
        with isolated_app() as (root, app, errors):
            configure(app)
            proc = Mock()
            with patch.object(gui, "port_in_use", return_value=False), \
                 patch.object(gui, "bindable_gateway_port", return_value=18899), \
                 patch.object(gui.subprocess, "Popen", return_value=proc) as spawn, \
                 patch.object(app, "_gateway_started") as started:
                app._start_gateway()
                pump(root, .15)
                started.assert_called_once()
                args = spawn.call_args.args[0]
                self.assertEqual(args[args.index("--port") + 1], "18899")
                self.assertEqual(app.port_var.get(), "18899")
                self.assertEqual(app.gateway_url, "http://127.0.0.1:18899")
                self.assertEqual(app.client.base_url, app.gateway_url)
                self.assertEqual(errors, [])

    def test_stalled_launch_expires_and_late_process_is_disposed(self):
        with isolated_app() as (root, app, errors):
            configure(app)
            entered, release, killed = threading.Event(), threading.Event(), threading.Event()
            proc = Mock()
            proc.poll.return_value = None

            def spawn(*args, **kwargs):
                entered.set()
                release.wait(2)
                return proc

            try:
                with patch.object(gui, "GATEWAY_LAUNCH_TIMEOUT_MS", 60, create=True), \
                     patch.object(gui, "port_in_use", return_value=False), \
                     patch.object(gui, "bindable_gateway_port", side_effect=lambda port: port), \
                     patch.object(gui.subprocess, "Popen", side_effect=spawn), \
                     patch.object(gui, "kill_process_tree", side_effect=lambda p: killed.set()), \
                     patch.object(app, "_gateway_started") as started:
                    app._start_gateway()
                    self.assertTrue(entered.wait(1))
                    pump(root, .2)
                    self.assertFalse(app._gateway_starting)
                    self.assertEqual(str(app.gw_btn.cget("state")), "normal")
                    self.assertIn("超时", app.status_var.get())
                    release.set()
                    self.assertTrue(killed.wait(1))
                    pump(root, .1)
                    started.assert_not_called()
                    self.assertIsNone(app.gateway_proc)
                    self.assertEqual(errors, [])
            finally:
                release.set()
                app.gateway_proc = None

    def test_stop_restores_controls_while_port_probe_is_stalled(self):
        with isolated_app() as (root, app, errors):
            configure(app)
            entered, release = threading.Event(), threading.Event()

            def check(*args):
                entered.set()
                release.wait(2)
                return False

            try:
                with patch.object(gui, "port_in_use", side_effect=check), \
                     patch.object(gui.subprocess, "Popen") as spawn:
                    app._start_gateway()
                    self.assertTrue(entered.wait(1))
                    app._stop_gateway_from_menu()
                    self.assertFalse(app._gateway_starting)
                    self.assertEqual(str(app.gw_btn.cget("state")), "normal")
                    release.set()
                    pump(root, .1)
                    spawn.assert_not_called()
                    self.assertEqual(errors, [])
            finally:
                release.set()

    def test_old_completion_cannot_reset_new_launch(self):
        with isolated_app() as (root, app, errors):
            old, current = threading.Event(), threading.Event()
            old.set()
            app._gateway_launch_cancel = current
            app._gateway_starting = True
            app.status_var.set("new launch")
            app._finish_deferred_gateway(None, {}, old, "old failure", True)
            self.assertTrue(app._gateway_starting)
            self.assertEqual(app.status_var.get(), "new launch")
            self.assertEqual(errors, [])

    def test_close_cleans_all_children_waiting_for_tk_handoff(self):
        with isolated_app() as (root, app, errors):
            first, second = Mock(), Mock()
            for proc in (first, second):
                proc.poll.return_value = None
            app._gateway_pending_procs = [first, second]
            app._sysmon = None

            def kill(proc):
                proc.poll.return_value = 1
                return True

            with patch.object(gui, "kill_process_tree", side_effect=kill) as dispose, \
                 patch.object(app, "_archive_current_session"), \
                 patch.object(root, "destroy") as destroy:
                app._on_close()
                pump(root, .15)
                self.assertEqual(dispose.call_count, 2)
                self.assertEqual({c.args[0] for c in dispose.call_args_list}, {first, second})
                self.assertEqual(app._gateway_pending_procs, [])
                destroy.assert_called_once()
                self.assertEqual(errors, [])

    def test_creation_is_not_readiness_and_does_not_reset_attempts(self):
        with isolated_app() as (root, app, errors):
            app._autostart_attempts = 4
            with patch.object(app, "_gateway_started"):
                app._finish_deferred_gateway(Mock(), {}, app._gateway_launch_cancel, "", True)
            self.assertEqual(app._autostart_attempts, 4)
            proc = Mock()
            proc.poll.return_value = None
            app.gateway_proc = proc
            app._gateway_up(proc)
            self.assertEqual(app._autostart_attempts, 0)
            app.gateway_proc = None

    def test_early_exit_uses_bounded_startup_retry_even_when_crashes_are_slow(self):
        with isolated_app() as (root, app, errors):
            proc = Mock(returncode=1)
            proc.poll.return_value = 1
            app.gateway_proc = proc
            app._gateway_autostarted = True
            app._autostart_attempts = 6
            with patch.object(gui, "_autostart_enabled", return_value=True):
                app._gateway_exited(proc)
            self.assertIsNone(app._autostart_after_id)
            self.assertIn("自动启动失败", app.status_var.get())
            self.assertEqual(errors, [])

    def test_health_timeout_does_not_kill_on_tk_thread(self):
        with isolated_app() as (root, app, errors):
            proc = Mock()
            proc.poll.return_value = None
            app.gateway_proc = proc
            app._gateway_autostarted = True
            entered, release = threading.Event(), threading.Event()
            threads = []

            def kill(p):
                threads.append(threading.get_ident())
                entered.set()
                release.wait(.4)
                proc.poll.return_value = 1
                return True

            try:
                with patch.object(gui, "kill_process_tree", side_effect=kill), \
                     patch.object(app, "_schedule_autostart_retry"):
                    begin = time.monotonic()
                    app._gateway_timeout(proc)
                    self.assertLess(time.monotonic() - begin, .15)
                    self.assertTrue(entered.wait(1))
                    self.assertNotIn(threading.get_ident(), threads)
                    heartbeat = []
                    root.after(1, lambda: heartbeat.append(True))
                    pump(root, .05)
                    self.assertTrue(heartbeat)
                    release.set()
                    pump(root, .15)
                    self.assertEqual(errors, [])
            finally:
                release.set()
                app.gateway_proc = None


if __name__ == "__main__":
    unittest.main()
