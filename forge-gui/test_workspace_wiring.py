"""W3 双工作空间接线测试（真实 Tk；临时 home，不碰真实用户数据）。

覆盖：Work/Coding 往返（对象/草稿/布局保持）、development 导航 adapter、
模式持久化与启动恢复、P0-01 关闭保护、P0-03 回归、Web UI P0-02。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk  # noqa: E402

import forge_gui_v2 as gui  # noqa: E402
import secret_store  # noqa: E402
import sub_agent  # noqa: E402
from test_layout_dpi import pump, isolated_app  # noqa: E402


class WorkspaceWiringTests(unittest.TestCase):
    def test_coding_first_send_uses_host_models_with_separate_policy_and_session(self):
        from forge.development.models import ModelSettingsSource
        from forge.development.runtime import DevelopmentRuntime
        from forge.model import Usage
        original_source, initialize = ModelSettingsSource.__call__, DevelopmentRuntime.initialize
        calls = []
        class Transport:
            def complete(_, provider, model, messages, **options):
                calls.append((provider.name, model))
                return 'hosted response', Usage(5, 5, provider.name, model)
        def source(owner, config, home):
            router = original_source(owner, config, home)
            router.transport = Transport()
            return router
        def init(runtime, project):
            result = initialize(runtime, project)
            runtime.bridge.agent.thinking = False
            runtime.bridge.agent.thinking_mode = 'off'
            return result
        with isolated_app() as (root, app, errors), patch.object(ModelSettingsSource,'__call__',source), patch.object(DevelopmentRuntime,'initialize',init):
            project = app.home/'project'
            project.mkdir()
            app._request_workspace(app._workspace_kind.CODING)
            bench = app._coding_bench
            self.assertEqual(bench.state.mode.value, 'development')
            bench.planning.set('none')
            bench.input.insert('1.0','hosted first send')
            with patch('development_mode.app.filedialog.askdirectory',return_value=str(project)):
                bench.send_button.invoke()
            deadline = time.monotonic()+30
            while time.monotonic()<deadline:
                pump(root,.04)
                if bench.runtime.tasks and not bench._jobs and not bench._submission_active:
                    break
            self.assertEqual(len(bench.runtime.tasks),1,bench.status.get()+'\n'+bench.chat.get('1.0','end'))
            self.assertEqual(calls,[('mimo','mimo-v2.6-flash-long-model-name-for-layout-testing')])
            self.assertNotEqual(bench.runtime.home, app.home)
            self.assertEqual(bench.runtime.bridge.agent.policy.mode.value,'read-only')
            self.assertTrue(bench.runtime.bridge.agent.session.path.is_relative_to(bench.runtime.home))
            self.assertEqual(errors,[])

    def test_roundtrip_preserves_surfaces_draft_and_objects(self):
        with isolated_app() as (root, app, errors):
            work_frame = app._workspace_deck.frames[app._workspace_kind.WORK]
            self.assertEqual(app._workspace_mode, app._workspace_kind.WORK)
            # 草稿进入输入框后切换
            app.input_card.entry.insert("1.0", "未发送的草稿")
            app._request_workspace(app._workspace_kind.CODING)
            pump(root)
            self.assertIsNotNone(app._coding_bench)
            coding_frame = app._workspace_deck.frames[app._workspace_kind.CODING]
            # work surface 隐藏但存活；coding surface 可见
            self.assertEqual(work_frame.winfo_manager(), "")
            self.assertEqual(coding_frame.winfo_manager(), "grid")
            bench_first = app._coding_bench
            # 往返：对象不重建（deck 只改可见性）
            app._request_workspace(app._workspace_kind.WORK)
            pump(root)
            self.assertEqual(work_frame.winfo_manager(), "grid")
            self.assertIs(app._coding_bench, bench_first, "往返不得重建工作台")
            self.assertIn("未发送的草稿", app.input_card.entry.get("1.0", "end"))
            # 再进 coding：同一 bench
            app._request_workspace(app._workspace_kind.CODING)
            pump(root)
            self.assertIs(app._coding_bench, bench_first)
            self.assertEqual(errors, [])

    def test_development_nav_adapter_requests_coding(self):
        with isolated_app() as (root, app, errors):
            app._nav_click("development")
            pump(root)
            self.assertEqual(app._workspace_mode, app._workspace_kind.CODING)
            self.assertIsNotNone(app._coding_bench)
            # 旧 _show_view 路径同样转发，不产生第二次挂载
            app._request_workspace(app._workspace_kind.WORK)
            pump(root)
            bench = app._coding_bench
            app._show_view("development")
            pump(root)
            self.assertIs(app._coding_bench, bench)
            self.assertEqual(errors, [])

    def test_workspace_mode_persisted_and_restored(self):
        with isolated_app() as (root, app, errors):
            # 切换 Coding → 偏好落盘（desktop config）
            app._request_workspace(app._workspace_kind.CODING)
            pump(root)
            saved = json.loads(gui._desktop_config_path().read_text(encoding="utf-8"))
            self.assertEqual(saved.get("workspace_mode"), "coding")
            # 恢复逻辑：模拟新进程读到该偏好（挂载工作台、切到 Coding）
            app._request_workspace(app._workspace_kind.WORK)
            pump(root)
            with patch.object(gui, "load_desktop_config",
                              return_value={"workspace_mode": "coding"}):
                app._restore_workspace_preference()
            pump(root)
            self.assertEqual(app._workspace_mode, app._workspace_kind.CODING)
            self.assertIsNotNone(app._coding_bench)
            self.assertEqual(errors, [])
            # 偏好缺失/异常时留在 Work（不抛、不切换）
            app._request_workspace(app._workspace_kind.WORK)
            pump(root)
            with patch.object(gui, "load_desktop_config", side_effect=OSError("boom")):
                app._restore_workspace_preference()
            self.assertEqual(app._workspace_mode, app._workspace_kind.WORK)

    def test_on_close_honours_bench_can_close(self):
        with isolated_app() as (root, app, errors):
            app._request_workspace(app._workspace_kind.CODING)
            pump(root)
            calls = {"close": 0, "layout": 0}

            def refuse():
                calls["close"] += 1
                return False

            def layout():
                calls["layout"] += 1

            app._coding_bench.can_close = refuse
            app._coding_bench.capture_layout = layout
            app._on_close()   # bench 拒绝 → 整个退出中止，窗口保留
            pump(root)
            self.assertTrue(root.winfo_exists())
            self.assertEqual(calls["close"], 1)
            self.assertEqual(calls["layout"], 0)

            def allow():
                calls["close"] += 1
                return True

            app._coding_bench.can_close = allow
            app._feature_dirty = False
            app._team_dirty = False
            app._pending_rows = []
            with patch.object(app, "_editor_is_dirty", return_value=False):
                with patch.object(app, "_close_for_real"):
                    app._on_close()
            pump(root)
            self.assertEqual(calls["close"], 2)
            self.assertEqual(calls["layout"], 1, "确认退出后补记布局快照")

    def test_coding_surface_uses_isolated_dev_home(self):
        with isolated_app() as (root, app, errors):
            app._request_workspace(app._workspace_kind.CODING)
            pump(root)
            # 隔离语义保持：独立 home，宿主不传 runtime
            self.assertTrue(app._coding_bench._owns_runtime)
            home = Path(app._coding_bench.state.path).parent
            self.assertEqual(home.name, ".forge-development")
            self.assertEqual(errors, [])


class P0Regressions(unittest.TestCase):
    def test_teamtoggle_default_bg_no_nameerror(self):
        """P0-03：省略 bg 时 TeamTogglePill 不得 NameError（_bg_of 已导入）。"""
        import chat_widgets as cw
        root = tk.Tk()
        root.withdraw()
        try:
            pill = cw.TeamTogglePill(root, on_change=lambda v: None)
            self.assertIsNotNone(pill)
        finally:
            root.destroy()

    def test_tool_detail_bounded_and_informative(self):
        detail = gui._tool_detail("read_file", {"path": "a" * 500},
                                  {"ok": True, "content": "x" * 900})
        self.assertIn("read_file", detail)
        self.assertIn("→", detail)
        self.assertLessEqual(len(detail), 640)

    def test_tool_rows_carry_numeric_elapsed_and_detail(self):
        """工具行契约：elapsed 为数值（ToolCard 汇总才不为 0）且带 detail。"""
        import chat_widgets as cw
        self.assertEqual(cw._guess_seconds_from_row({"elapsed": 1.5}), 1.5)
        self.assertEqual(cw._guess_seconds_from_row({"elapsed": "第1轮"}), 0.0)


class WebServerP0Tests(unittest.TestCase):
    """P0-02：/api/models 可用、凭据走 secrets 机制、不泄露键值。"""

    @contextmanager
    def _server(self, secrets: dict | None):
        import threading
        import client.server as cs

        class _QuietServer(cs.ClientServer):
            # daemon 线程在解释器退出时被 GC 打断会触发 Windows 原生异常
            # （0x80000003）；非 daemon + server_close 的 block_on_close 会
            # 在关闭时 join 全部 handler 线程，测试进程干净退出。
            daemon_threads = False

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            secrets_file = home / "secrets.json"
            if secrets is not None:
                secrets_file.write_text(json.dumps(secrets), encoding="utf-8")
            with patch.object(cs, "SECRETS_FILE", secrets_file), \
                 patch.object(cs, "FORGE_HOME", home):
                srv = _QuietServer(("127.0.0.1", 0), cs.Handler)
                port = srv.server_address[1]
                thread = threading.Thread(target=srv.serve_forever, daemon=True)
                thread.start()
                try:
                    yield f"http://127.0.0.1:{port}", cs
                finally:
                    srv.shutdown()
                    srv.server_close()
                    thread.join(5)

    def _get(self, url, path):
        import urllib.request
        with urllib.request.urlopen(url + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_models_endpoint_ok_and_keys_not_leaked(self):
        with self._server({"deepseek": "sk-test-value-123"}) as (url, cs):
            data = self._get(url, "/api/models")
            self.assertIn("models", data)
            deepseek = next(m for m in data["models"] if m["id"] == "deepseek")
            self.assertTrue(deepseek["keyReady"])
            self.assertNotIn("sk-test-value-123", json.dumps(data))

    def test_diag_reports_secrets_store(self):
        with self._server(None) as (url, cs):
            data = self._get(url, "/api/diag")
            self.assertIn("secrets.json", data["keys_file"])
            self.assertFalse(data["keys_exists"], "无 secrets.json 时应如实报告缺失")

    def test_provider_row_resolves_key_in_memory(self):
        import client.server as cs
        with tempfile.TemporaryDirectory() as tmp:
            secrets_file = Path(tmp) / "secrets.json"
            secrets_file.write_text(json.dumps({"deepseek": "sk-live"}), encoding="utf-8")
            with patch.object(cs, "SECRETS_FILE", secrets_file):
                hit = next(m for m in cs.CATALOG if m["id"] == "deepseek")
                row = cs._resolved_provider_row(hit)
                self.assertEqual(row["config"]["apiKey"], "sk-live")
                # 未配置密钥的 provider：保留 $expr（诚实解析为空），不伪造
                miss = next(m for m in cs.CATALOG if m["id"] == "glm")
                row2 = cs._resolved_provider_row(miss)
                self.assertIn("$expr", row2["config"]["apiKey"])
                # 源模板未被就地污染
                self.assertIn("$expr", cs.PROVIDER_ROWS["deepseek"]["config"]["apiKey"])

    def test_load_keys_maps_secrets_names_to_env(self):
        import client.server as cs
        with tempfile.TemporaryDirectory() as tmp:
            secrets_file = Path(tmp) / "secrets.json"
            secrets_file.write_text(json.dumps({
                "deepseek": "k1", "my-provider": "k2", "bad name!": "k3", "n": ""}),
                encoding="utf-8")
            with patch.object(cs, "SECRETS_FILE", secrets_file):
                keys = cs.load_keys()
            self.assertEqual(keys, {"FORGE_DEEPSEEK_KEY": "k1",
                                    "FORGE_MY_PROVIDER_KEY": "k2"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
