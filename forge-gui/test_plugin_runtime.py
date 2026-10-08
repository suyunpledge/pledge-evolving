"""plugin_runtime 的单元测试（全部临时 home，绝不碰真实 ~/.forge）。"""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import plugin_market as pm    # noqa: E402
import plugin_runtime as pr   # noqa: E402


def make_plugin(market: pm.Marketplace, pid: str, *, body: str,
                **manifest) -> Path:
    """用**传入的 market 实例**落一个带代码体的插件并安装+ack+启用。

    状态按家目录加锁并从磁盘刷新；不同 Marketplace 实例也能看到相同状态。
    这里使用调用方实例，便于检查每一步生命周期。
    """
    home = market.home
    src = home / "_src" / pid
    src.mkdir(parents=True, exist_ok=True)
    payload = {"id": pid, "name": pid, "version": "1.0.0",
               "executes_code": True, **manifest}
    (src / pm.MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (src / "plugin.py").write_text(textwrap.dedent(body), encoding="utf-8")
    market.install_from_dir(src, enable=True)
    if market.find(pid).needs_ack:
        market.ack(pid)          # 代码插件：测试里默认信任，ack 闸门单独有用例覆盖
        market.enable(pid)
    return market.plugins_dir / pid


GOOD_PLUGIN = """
        def register():
            def add(args):
                return {"sum": args.get("a", 0) + args.get("b", 0)}
            return [{"name": "demo_add", "description": "加法",
                     "parameters": {"type": "object",
                                    "properties": {"a": {"type": "number"},
                                                    "b": {"type": "number"}}},
                     "execute": add}]
"""

BAD_NAME_PLUGIN = """
        def register():
            return [{"name": "1bad name", "description": "", "execute": lambda a: 1}]
"""

NO_EXEC_PLUGIN = """
        def register():
            return [{"name": "noexec", "execute": "不是函数"}]
"""

NO_REGISTER_PLUGIN = """
        X = 1
"""

CRASH_REGISTER_PLUGIN = """
        def register():
            raise ValueError("register 炸了")
"""

TOOL_CRASH_PLUGIN = """
        def register():
            def boom(args):
                raise RuntimeError("故意炸")
            return [{"name": "boom_tool", "execute": boom}]
"""

SLEEP_PLUGIN = """
        import time as _t
        def register():
            def slow(args):
                _t.sleep(30)
            return [{"name": "slow_tool", "execute": slow}]
"""

MANY_TOOLS_PLUGIN = """
        def register():
            out = []
            for i in range(40):
                out.append({"name": f"tool_{i:02d}", "execute": (lambda a, _i=i: _i)})
            return out
"""

BIG_RESULT_PLUGIN = """
        def register():
            def big(args):
                return "x" * 500_000
            return [{"name": "big_tool", "execute": big}]
"""


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forge-rt-")
        self.home = Path(self._tmp.name)
        self.market = pm.Marketplace(home=self.home)
        self.rt = pr.PluginRuntime(self.market, secret_isolation=False)

    def tearDown(self):
        self._tmp.cleanup()

    # ── 基本加载 ────────────────────────────────────────────────────

    def test_load_enabled_plugin_and_call_tool(self):
        make_plugin(self.market, "adder", body=GOOD_PLUGIN)
        report = self.rt.reload()
        self.assertEqual(report.loaded, ["adder"])
        self.assertIn("demo_add", self.rt.names())
        out = self.rt.call("demo_add", {"a": 2, "b": 40})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"], {"sum": 42})

    def test_disabled_plugin_not_loaded(self):
        make_plugin(self.market, "quiet", body=GOOD_PLUGIN)
        self.market.disable("quiet")
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        self.assertEqual(self.rt.names(), [])

    # ── ack 闸门 ────────────────────────────────────────────────────

    def test_no_ack_means_not_imported(self):
        """executes_code=true 且没 ack → 不能 import、不能调用。"""
        src = self.home / "_src" / "noack"
        src.mkdir(parents=True, exist_ok=True)
        (src / pm.MANIFEST_NAME).write_text(json.dumps(
            {"id": "noack", "name": "n", "version": "1.0.0",
             "executes_code": True}), encoding="utf-8")
        (src / "plugin.py").write_text("def register(): return []", encoding="utf-8")
        self.market.install_from_dir(src)   # 只装不 ack
        # enable 会因缺 ack 被拒 —— 直接改状态模拟"状态被手动改过"的场景
        self.market._enable_in_state("noack")
        self.market.save()
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        reasons = dict(report.skipped)
        self.assertIn("信任确认", reasons.get("noack", ""))
        self.assertEqual(self.rt.names(), [])

    def test_acked_plugin_loads(self):
        make_plugin(self.market, "acked", body=GOOD_PLUGIN)
        self.market.ack("acked")
        report = self.rt.reload()
        self.assertIn("acked", report.loaded)
        self.assertIn("demo_add", self.rt.names())

    # ── 校验 ────────────────────────────────────────────────────────

    def test_bad_tool_name_rejected(self):
        make_plugin(self.market, "badname", body=BAD_NAME_PLUGIN)
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        self.assertTrue(report.errors and "工具名不合法" in report.errors[0][1])

    def test_non_callable_execute_rejected(self):
        make_plugin(self.market, "noexec", body=NO_EXEC_PLUGIN)
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        self.assertTrue(any("execute" in msg for _, msg in report.errors))

    def test_missing_register_reported(self):
        make_plugin(self.market, "noreg", body=NO_REGISTER_PLUGIN)
        report = self.rt.reload()
        self.assertTrue(any("register()" in msg for _, msg in report.errors))

    def test_register_crash_isolated(self):
        """一个插件炸不能拖垮另一个。"""
        make_plugin(self.market, "crashy", body=CRASH_REGISTER_PLUGIN)
        make_plugin(self.market, "healthy", body=GOOD_PLUGIN)
        report = self.rt.reload()
        self.assertEqual(report.loaded, ["healthy"])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0][0], "crashy")
        self.assertIn("demo_add", self.rt.names())

    def test_missing_entry_file_reported(self):
        src = self.market.home / "_src" / "noentry"
        src.mkdir(parents=True, exist_ok=True)
        (src / pm.MANIFEST_NAME).write_text(json.dumps(
            {"id": "noentry", "name": "n", "version": "1.0.0",
             "executes_code": True}), encoding="utf-8")
        self.market.install_from_dir(src, enable=True)
        self.market.ack("noentry")
        self.market.enable("noentry")   # ack 后手动启用（install 时被 ack 闸门拦了）
        report = self.rt.reload()
        self.assertTrue(any("plugin.py" in msg for _, msg in report.errors),
                        report.errors)

    def test_declonly_plugin_skipped(self):
        """声明式条目（executes_code=false）没有可执行体，跳过不报错。"""
        self.market.install_from_catalog("tool-web-search", enable=True)
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        self.assertTrue(any("声明式" in msg for _, msg in report.skipped))

    # ── 调用安全 ────────────────────────────────────────────────────

    def test_tool_crash_returns_error_not_raise(self):
        make_plugin(self.market, "boomer", body=TOOL_CRASH_PLUGIN)
        self.rt.reload()
        out = self.rt.call("boom_tool", {})
        self.assertFalse(out["ok"])
        self.assertIn("故意炸", out["error"])

    def test_unknown_tool(self):
        out = self.rt.call("不存在", {})
        self.assertFalse(out["ok"])

    def test_timeout_kills_call(self):
        make_plugin(self.market, "sleeper", body=SLEEP_PLUGIN)
        rt = pr.PluginRuntime(self.market, exec_timeout=0.5, secret_isolation=False)
        rt.reload()
        import time
        t = time.perf_counter()
        out = rt.call("slow_tool", {})
        took = time.perf_counter() - t
        self.assertFalse(out["ok"])
        self.assertIn("超过", out["error"])
        self.assertLess(took, 5, "超时应尽快返回，不该等满 30s")

    def test_huge_result_capped(self):
        make_plugin(self.market, "biggie", body=BIG_RESULT_PLUGIN)
        self.rt.reload()
        out = self.rt.call("big_tool", {})
        self.assertTrue(out["ok"])
        text = json.dumps(out["result"], ensure_ascii=False)
        self.assertLess(len(text), pr.MAX_RESULT_CHARS + 200)
        self.assertIn("截断", text)

    def test_tool_count_capped(self):
        make_plugin(self.market, "many", body=MANY_TOOLS_PLUGIN)
        report = self.rt.reload()
        self.assertLessEqual(len(self.rt.names()), pr.MAX_TOOLS_PER_PLUGIN)
        self.assertTrue(any("上限" in msg for _, msg in report.skipped))

    def test_arguments_must_be_dict(self):
        make_plugin(self.market, "adder2", body=GOOD_PLUGIN)
        self.rt.reload()
        out = self.rt.call("demo_add", [1, 2])  # type: ignore[arg-type]
        self.assertFalse(out["ok"])
        self.assertIn("对象", out["error"])

    # ── schema 导出 ─────────────────────────────────────────────────

    def test_openai_schema_shape(self):
        make_plugin(self.market, "schema", body=GOOD_PLUGIN)
        self.rt.reload()
        schemas = self.rt.openai_schemas()
        self.assertEqual(len(schemas), 1)
        fn = schemas[0]["function"]
        self.assertEqual(fn["name"], "demo_add")
        self.assertEqual(fn["parameters"]["type"], "object")
        self.assertIn("properties", fn["parameters"])

    # ── reload 幂等 ─────────────────────────────────────────────────

    def test_reload_replaces_tools(self):
        make_plugin(self.market, "first", body=GOOD_PLUGIN)
        self.rt.reload()
        self.market.uninstall("first")
        report = self.rt.reload()
        self.assertEqual(report.loaded, [])
        self.assertEqual(self.rt.names(), [], "卸载后 reload 应清掉旧工具")


class NoPollutionCase(unittest.TestCase):
    def test_plugin_modules_not_in_sys_modules(self):
        """加载完不往 sys.modules 塞模块（防命名空间污染）。"""
        with tempfile.TemporaryDirectory(prefix="forge-np-") as td:
            home = Path(td)
            market = pm.Marketplace(home=home)
            make_plugin(market, "pollute", body=GOOD_PLUGIN)
            rt = pr.PluginRuntime(market, secret_isolation=False)
            rt.reload()
            leaked = [k for k in sys.modules if k.startswith("forge_plugin_")]
            self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
