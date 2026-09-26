"""Offline interaction regressions. All sessions and Git writes use temporary directories."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import forge_gui_v2 as gui
import workspace as ws
from forge_client import ChatMessage, ForgeGatewayClient, GenerationCancelled
from interaction_model import (read_attachment, compose_prompt, task_command, task_outcome,
                               gateway_settings, select_provider)


class ContractTests(unittest.TestCase):
    def test_gateway_settings_use_real_url_and_resolve_only_env_reference(self):
        provider = {"baseURL": "https://example.invalid/v1/", "model": "actual-model",
                    "wire": "openai", "apiKey": {"$expr": "get('env.TEST_KEY', '')"}}
        self.assertEqual(gateway_settings(provider, {"TEST_KEY": "test-value"}),
                         ("https://example.invalid/v1", "test-value", "actual-model"))
        with self.assertRaises(ValueError):
            gateway_settings(provider, {})
        rows = [{"disabled": True, "config": dict(provider, model="disabled")}, {"config": provider}]
        self.assertEqual(select_provider(rows), provider)
        self.assertIsNone(select_provider(rows, "disabled"))
        with self.assertRaises(ValueError):
            gateway_settings(dict(provider, wire="anthropic"), {"TEST_KEY": "test"})

    def test_cli_forwards_client_wire_without_changing_legacy_default(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from forge.cli import build_parser, cmd_gateway
        parser = build_parser()
        base = ["gateway", "--upstream", "http://127.0.0.1:1/v1"]
        self.assertEqual(parser.parse_args(base).client_wire, "anthropic")
        args = parser.parse_args(base + ["--client-wire", "openai", "--upstream-wire", "openai"])
        args.workspace = "."
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch("forge.gateway.serve", return_value=server) as serve:
            self.assertEqual(cmd_gateway(args), 0)
        self.assertEqual(serve.call_args.args[0].wire, "openai")

    def test_strategies_use_the_routing_flag(self):
        for strategy in ("economy", "balanced", "premium"):
            args = task_command("python", Path("repo/run.py"), "home", "task", strategy)
            self.assertEqual(args[args.index("--strategy") + 1], strategy)
            self.assertNotIn("--profile", args)
            self.assertEqual(args[args.index("--workspace") + 1], "repo")

    def test_framework_final_is_not_early_stop_and_zero_exit_can_be_error(self):
        self.assertEqual(task_outcome(0, {"stopped": "final"})[1], "ok")
        self.assertEqual(task_outcome(0, {"stopped": "error"})[1], "error")
        self.assertEqual(task_outcome(0, {"stopped": "max_steps"})[1], "warn")
        self.assertEqual(task_outcome(0, {"stopped": "final"}, True)[0], "已停止")

    def test_attachment_is_a_real_snapshot_and_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "中文 文件.txt"
            path.write_text("实际文件内容", encoding="utf-8")
            attachment = read_attachment(path)
            path.write_text("later", encoding="utf-8")
            self.assertIn("实际文件内容", compose_prompt("问题", [attachment]))
            path.write_bytes(b"\x00data")
            with self.assertRaises(ValueError):
                read_attachment(path)
            path.write_bytes(b"a" * (128 * 1024 + 1))
            with self.assertRaises(ValueError):
                read_attachment(path)

    def test_cancel_stops_sse_and_callback_errors_propagate(self):
        event = threading.Event()
        chunk = b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
        chunks = []
        def receive(piece):
            chunks.append(piece)
            event.set()
        with patch("forge_client.urllib.request.urlopen", return_value=io.BytesIO(chunk * 3)):
            with self.assertRaises(GenerationCancelled):
                ForgeGatewayClient().stream_chat([], on_chunk=receive, cancel_event=event)
        self.assertEqual(chunks, ["x"])
        with patch("forge_client.urllib.request.urlopen", return_value=io.BytesIO(chunk)):
            with self.assertRaises(ValueError):
                ForgeGatewayClient().stream_chat([], on_chunk=lambda _: (_ for _ in ()).throw(ValueError()))


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_unicode_staged_diff_untracked_rename_and_clean_file(self):
        path = self.repo / "中文 文件.py"
        path.write_text("old\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        path.write_text("new\n", encoding="utf-8")
        self.git("add", ".")
        self.assertEqual(ws._git_status_map(self.repo)[path.name], "M")
        self.assertIn("+new", ws._git_diff(self.repo, path=path))
        self.assertEqual(ws._git_diff_numstat(self.repo)[path.name], (1, 1))
        self.git("commit", "-qm", "update")
        self.assertEqual(ws._git_diff(self.repo, path=path), "")
        self.git("mv", path.name, "重命名.py")
        (self.repo / "新增.txt").write_text("new file\n", encoding="utf-8")
        statuses = ws._git_status_map(self.repo)
        self.assertEqual(statuses["重命名.py"], "R")
        self.assertEqual(statuses["新增.txt"], "??")
        self.assertEqual(len(statuses), 2)

    def test_staged_files_in_unborn_repository(self):
        (self.repo / "first.txt").write_text("first\n", encoding="utf-8")
        self.git("add", ".")
        self.assertIn("+first", ws._git_diff(self.repo))
        self.assertEqual(ws._git_diff_numstat(self.repo)["first.txt"], (1, 0))

    def test_collapsed_directories_do_not_expose_descendants(self):
        directory = self.repo / "folder"
        directory.mkdir()
        child = directory / "inner.txt"
        child.touch()
        paths = [n["path"] for n in ws._scan_tree(self.repo, expanded={str(self.repo)})]
        self.assertIn(directory, paths)
        self.assertNotIn(child, paths)
        paths = [n["path"] for n in ws._scan_tree(self.repo, expanded={str(self.repo), str(directory)})]
        self.assertIn(child, paths)


class GuiInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gui._setup_dpi()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        with patch.object(gui, "DEFAULT_FORGE_HOME", Path(self.tmp.name)), \
             patch.object(gui, "_find_run_py", return_value=None), \
             patch.object(gui.ForgeGuiApp, "_repo_root", return_value=Path(self.tmp.name)), \
             patch.object(gui.ForgeGuiApp, "_start_sysmon"), \
             patch.object(gui, "_autostart_enabled", return_value=False):
            self.app = gui.ForgeGuiApp(self.root)

    def tearDown(self):
        self.app._closing = True
        for attr in ("_autostart_after_id", "_event_poll"):
            aid = getattr(self.app, attr, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
        self.root.destroy()
        self.tmp.cleanup()
        self.assertEqual(self.errors, [])

    def pump(self, predicate):
        until = time.monotonic() + 3
        while not predicate() and time.monotonic() < until:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_empty_start_and_title_survives_reload(self):
        self.assertEqual(self.app._chat_history, [])
        self.assertEqual(self.app._load_sessions(), [])
        self.app._chat_history = [ChatMessage("user", "actual test message")]
        self.app._session_custom_title = "我的名称"
        sid = self.app._session_id
        self.app._archive_current_session()
        self.app._new_session()
        self.app._load_session(sid)
        self.assertEqual(self.app._session_title(), "我的名称")
        self.assertEqual(self.app._chat_history[0].content, "actual test message")

    def test_gateway_launch_uses_provider_and_keeps_key_off_command_line(self):
        self.app.run_py = Path(self.tmp.name) / "run.py"
        self.app.user_rows = [{"id": "test", "config": {
            "wire": "openai", "baseURL": "https://example.invalid/v1",
            "model": "test-model", "apiKey": {"$expr": "get('env.TEST_KEY', '')"}}}]
        proc = Mock()
        with patch.object(gui, "env_for", return_value={"TEST_KEY": "test-secret"}), \
             patch.object(gui.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(gui.threading, "Thread"):
            self.app._start_gateway()
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--upstream") + 1], "https://example.invalid/v1")
        self.assertEqual(command[command.index("--client-wire") + 1], "openai")
        self.assertNotIn("test-secret", " ".join(command))
        self.assertEqual(popen.call_args.kwargs["env"]["FORGE_GATEWAY_KEY"], "test-secret")
        self.app.gateway_proc = None

    def test_context_off_only_sends_new_prompt_but_keeps_visible_history(self):
        self.app._chat_history = [ChatMessage("user", "old"), ChatMessage("assistant", "old response")]
        self.app._include_history = False
        captured = []
        class Client:
            def health(self): return True, "ok"
            def stream_chat(self, messages, **kwargs):
                captured.extend(messages)
                kwargs["on_chunk"]("new response")
        self.app.client = Client()
        self.app.send_var.set("new question")
        self.app._do_send()
        self.pump(lambda: not self.app._sending)
        self.assertEqual([m.content for m in captured], ["new question"])
        self.assertEqual(len(self.app._chat_history), 4)

    def test_cancel_during_health_does_not_send_or_save(self):
        release = threading.Event()
        class Client:
            called = False
            def health(self):
                release.wait(1)
                return True, "ok"
            def stream_chat(self, *_args, **_kwargs): self.called = True
        self.app.client = Client()
        self.app.send_var.set("cancel me")
        self.app._do_send()
        self.app._stop_send()
        release.set()
        self.pump(lambda: not self.app._sending)
        self.assertFalse(self.app.client.called)
        self.assertEqual(self.app._chat_history, [])
        self.assertEqual(self.app.send_var.get(), "cancel me")

    def test_multiline_input_and_local_command(self):
        self.app.send_var.set("first\nsecond")
        self.root.update()
        self.assertEqual(self.app.send_entry.get("1.0", "end-1c"), "first\nsecond")
        self.app.send_entry.insert(tk.END, "\nthird")
        self.root.update()
        self.assertEqual(self.app.send_var.get(), "first\nsecond\nthird")
        self.app.send_var.set("/tools")
        self.app._do_send()
        self.assertEqual(self.app._active_view, "tools")
        self.assertFalse(self.app._sending)

    def test_logs_survive_hidden_workspace_and_clean_file_is_not_added(self):
        panel = self.app.workspace
        panel.push_terminal("real test log")
        panel._set_preview_sub("终端")
        self.assertIn("real test log", panel._term_text.get("1.0", tk.END))
        self.assertEqual(str(panel._term_text["state"]), "disabled")
        path = Path(self.tmp.name) / "clean.py"
        path.write_text("unchanged", encoding="utf-8")
        panel._is_git_repo = True
        panel._git_status = {}
        with patch.object(ws, "_git_diff", return_value=""):
            panel._render_diff(path)
        self.assertNotIn("新增文件", panel._diff_text.get("1.0", tk.END))

    def test_narrow_window_keeps_composer_and_task_controls_reachable(self):
        self.root.deiconify()
        self.root.geometry("1120x720")
        self.app._open_workspace()
        self.root.update()
        for widget in (self.app.send_entry, self.app.model_combo, self.app.input_card.send_circle):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 self.app.center.winfo_rootx() + self.app.center.winfo_width())
        self.app.input_card.set_busy(True)
        self.root.update()
        self.assertTrue(self.app.input_card.stop_circle.winfo_ismapped())
        self.app._show_view("task")
        self.root.update()
        self.assertTrue(self.app.task_run_btn.winfo_ismapped())
        self.assertTrue(self.app.task_stop_btn.winfo_ismapped())

    def test_denied_tool_is_not_success_and_normal_report_is_not_early_stop(self):
        msg = Mock()
        self.app._task_msg = msg
        self.app._task_started = time.time()
        self.app._task_cancel_event = threading.Event()
        report = {"text": "actual report", "stopped": "final", "steps": [
            {"tool": "write_file", "decision": "deny", "index": 1, "result": "denied"}]}
        self.app._task_finished(0, json.dumps(report), "")
        self.assertFalse(msg.add_tool_card.call_args.args[0][0]["ok"])
        notes = " ".join(call.args[0] for call in msg.add_note.call_args_list)
        self.assertNotIn("提前停止", notes)
        self.assertIn("执行结束", notes)


if __name__ == "__main__":
    unittest.main()
