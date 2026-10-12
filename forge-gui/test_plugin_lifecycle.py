"""插件完整生命周期的端到端测试（发现→安装→权限→加载→调用→禁用→卸载）。

与既有分域测试（market/dependencies/capabilities/runtime/skills/cli）互补：
这里把一条声明式插件从发现走到卸载，并覆盖本轮收敛引入的行为——
反向依赖级联禁用、降级安装击穿约束、reload 的运行时依赖复查。
全部在临时 home 下运行，绝不触碰真实 ~/.forge。
"""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_market as pm          # noqa: E402
import plugin_capabilities as pc    # noqa: E402
import plugin_dependencies as pdep  # noqa: E402
from forge.policy import Mode, Policy                     # noqa: E402
from forge.tools import ToolContext, build_builtin_registry  # noqa: E402


def write_plugin(folder: Path, pid: str, **fields) -> Path:
    """落一个声明式插件源目录（contributions 契约）。"""
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"id": pid, "name": pid, "version": "1.0.0"}
    payload.update(fields)
    (folder / pm.MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


READER_MANIFEST = dict(
    capabilities=["repo.read"],
    contributions={"tools": [{
        "name": "lifecycle_read", "target": "read_file",
        "description": "读取工作区文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"], "additionalProperties": False}}]},
)


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forge-lifecycle-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.ws = self.home / "repo"
        self.ws.mkdir()
        (self.ws / "note.txt").write_text("生命周期样本", encoding="utf-8")
        self.market = pm.Marketplace(self.home / "forge")
        # 真网关：内置注册表 + 只读 Policy（写路径必须被真实策略拒绝）
        self.gateway = build_builtin_registry()
        self.policy = Policy(mode=Mode.READ_ONLY, workspace=self.ws)
        self.dispatched: list[tuple[str, dict]] = []

    # -- helpers ---------------------------------------------------------

    def dispatch(self, name, args, **context):
        self.dispatched.append((name, args))
        return self.gateway.invoke(name.removeprefix("forge_"), args,
                                   ToolContext(self.policy, self.ws)).as_dict()

    def install_reader(self, pid="lifecycle-reader"):
        src = write_plugin(self.home / "_src" / pid, pid, **READER_MANIFEST)
        return self.market.install_from_dir(src)

    def runtime(self):
        rt = pc.CapabilityRuntime(self.market, self.ws, "lifecycle", self.dispatch)
        rt.reload(reserved_names=self.gateway.names())
        return rt


class TestFullLifecycle(LifecycleCase):
    """验收主线：发现 → 安装 → 权限检查 → 加载 → 实际调用 → 禁用 → 卸载。"""

    def test_discover_to_uninstall(self):
        # 1. 发现：内置目录可见且市场可枚举（离线内置源非空）
        catalog_ids = {p.id for p in self.market.catalog()}
        self.assertTrue(catalog_ids, "内置目录为空，发现面失效")
        self.assertIn("tool-web-search", catalog_ids)

        # 2. 安装：本地目录装进 plugins/，装完默认不启用（安装≠激活）
        plugin = self.install_reader()
        self.assertTrue(plugin.installed)
        self.assertFalse(plugin.enabled)
        self.assertTrue((self.market.plugins_dir / plugin.id).is_dir())

        # 3. 权限检查 a：未确认（ack）前 enable 被拒
        with self.assertRaises(PermissionError):
            self.market.enable(plugin.id)

        # 3. 权限检查 b：ack 后 enable，但未 grant 前 Agent 侧看不到工具
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        rt = self.runtime()
        self.assertEqual(rt.names(), [])

        # 3. 权限检查 c：grant 只能授清单声明过的能力
        with self.assertRaises(PermissionError):
            self.market.grant(plugin.id, ["repo.write"], self.ws)

        # 4. 加载 + 5. 实际调用：授予 repo.read 后经真实网关读文件
        self.market.grant(plugin.id, ["repo.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), ["lifecycle_read"])
        result = rt.call("lifecycle_read", {"path": "note.txt"})
        self.assertTrue(result["ok"], result)
        self.assertIn("生命周期样本", result["content"])
        self.assertEqual(self.dispatched, [("read_file", {"path": str((self.ws / "note.txt").resolve())})])
        # 调用进审计面（GUI/CLI 的状态一致性来源之一）
        audit = self.market.audit_tail(limit=10)
        self.assertTrue(any(row.get("tool") == "lifecycle_read" and row.get("outcome") == "allow"
                            for row in audit), audit)

        # 6. 禁用：工具立即失效，调用被拒
        self.market.disable(plugin.id)
        self.assertFalse(self.market.find(plugin.id).enabled)
        self.assertEqual(rt.names(), [])
        denied = rt.call("lifecycle_read", {"path": "note.txt"})
        self.assertFalse(denied["ok"])

        # 7. 卸载：目录进回收站，状态索引清除，目录里不再显示已装
        self.assertTrue(self.market.uninstall(plugin.id))
        self.assertFalse((self.market.plugins_dir / plugin.id).exists())
        self.assertNotIn(plugin.id, self.market.state()["installed"])
        gone = self.market.find(plugin.id)
        self.assertTrue(gone is None or not gone.installed)
        # 事件日志贯穿全程（安装/启用/禁用/卸载各留一行）
        events = [row["event"] for row in self.market.log_tail(limit=30)]
        for expected in ("install", "enable", "disable", "uninstall"):
            self.assertIn(expected, events)

    def test_permission_denial_by_real_policy(self):
        """声明写能力的插件：grant 了能力，网关真实 Policy（只读）仍拒绝。"""
        src = write_plugin(self.home / "_src" / "writer", "writer",
                           capabilities=["repo.write"],
                           contributions={"tools": [{
                               "name": "lifecycle_write", "target": "write_file",
                               "description": "写文件",
                               "parameters": {"type": "object",
                                              "properties": {"path": {"type": "string"},
                                                             "content": {"type": "string"}},
                                              "required": ["path", "content"],
                                              "additionalProperties": False}}]})
        plugin = self.market.install_from_dir(src)
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        self.market.grant(plugin.id, ["repo.write"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), ["lifecycle_write"])
        result = rt.call("lifecycle_write", {"path": "out.txt", "content": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("policy", result["error"])
        self.assertFalse((self.ws / "out.txt").exists(),
                         "被策略拒绝的写不允许落盘")

    def test_out_of_workspace_path_denied_before_dispatch(self):
        plugin = self.install_reader()
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        self.market.grant(plugin.id, ["repo.read"], self.ws)
        rt = self.runtime()
        outside = self.home / "neighbor.txt"
        outside.write_text("邻居", encoding="utf-8")
        result = rt.call("lifecycle_read", {"path": str(outside)})
        self.assertFalse(result["ok"])
        self.assertEqual(self.dispatched, [], "越界路径必须在派发前拒绝")


class TestReverseDependencyCascade(LifecycleCase):
    """卸载/降级安装时的反向依赖级联（本轮收敛新增）。"""

    def make_pair(self, base_version="2.0.0", dep_constraint=">=2.0"):
        base = write_plugin(self.home / "_src" / "base", "base",
                            version=base_version, capabilities=["skill.read"],
                            contributions={"skills": [{"name": "base_skill", "file": "SKILL.md"}]})
        (base / "SKILL.md").write_text("---\nname: base\n---\n基础能力", encoding="utf-8")
        dep = write_plugin(self.home / "_src" / "dependent", "dependent",
                           capabilities=["skill.read"],
                           requires={"plugins": {"base": dep_constraint}},
                           contributions={"skills": [{"name": "dep_skill", "file": "SKILL.md"}]})
        (dep / "SKILL.md").write_text("---\nname: dep\n---\n依赖 base", encoding="utf-8")
        return base, dep

    def ready(self, pid):
        p = self.market.install_from_dir(self.home / "_src" / pid)
        self.market.ack(pid, expected_fingerprint=p.ack_of())
        self.market.enable(pid)
        self.market.grant(pid, ["skill.read"], self.ws)
        return p

    def test_missing_dependency_blocks_install(self):
        base, dep = self.make_pair()
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.market.install_from_dir(dep)
        self.assertIn("未安装", str(ctx.exception))

    def test_uninstall_cascades_disable_to_dependents(self):
        base, dep = self.make_pair()
        self.ready("base")
        self.ready("dependent")
        self.assertTrue(self.market.find("dependent").enabled)

        self.market.uninstall("base")
        # 依赖方被联动禁用，且留痕
        self.assertFalse(self.market.find("dependent").enabled)
        events = [(row["event"], row["detail"]) for row in self.market.log_tail(limit=20)]
        self.assertTrue(any(e == "cascade-disable" and "dependent" in d for e, d in events),
                        events)
        # 目录还在（只是禁用），重新装回依赖后可以再启用
        self.assertTrue(self.market.find("dependent").installed)

    def test_disable_does_not_cascade_but_uninstall_does(self):
        """依赖契约只看安装集：禁用依赖不级联；卸载依赖必须级联。"""
        base, dep = self.make_pair()
        self.ready("base")
        self.ready("dependent")
        self.market.disable("base")
        # base 仍安装 → dependent 的约束未被破坏，保持启用
        self.assertTrue(self.market.find("dependent").enabled)
        # 卸载才离开安装集：此时级联禁用，且重新启用被闸门拦下
        self.market.enable("base")
        self.market.uninstall("base")
        self.assertFalse(self.market.find("dependent").enabled)
        with self.assertRaises(pdep.RequirementError):
            self.market.enable("dependent")

    def test_downgrade_reinstall_breaks_enabled_dependent(self):
        base, dep = self.make_pair(base_version="2.1.0")
        self.ready("base")
        self.ready("dependent")
        # 用 1.0.0 重装 base（降级），击穿 dependent 的 >=2.0 约束
        old = write_plugin(self.home / "_old" / "base", "base",
                           version="1.0.0", capabilities=["skill.read"],
                           contributions={"skills": [{"name": "base_skill", "file": "SKILL.md"}]})
        (old / "SKILL.md").write_text("---\nname: base\n---\n旧版", encoding="utf-8")
        self.market.install_from_dir(old)
        self.assertEqual(self.market.find("base").version, "1.0.0")
        self.assertFalse(self.market.find("dependent").enabled,
                         "降级击穿约束后依赖方必须被联动禁用")
        with self.assertRaises(pdep.RequirementError):
            self.market.enable("dependent")

    def test_reload_rechecks_dependencies_at_runtime(self):
        """状态被旁路改写（装后依赖被移走）时，reload 仍 fail-closed。"""
        base, dep = self.make_pair()
        self.ready("base")
        self.ready("dependent")
        rt = self.runtime()
        self.assertEqual(rt.names(), ["base_skill", "dep_skill"])
        # 旁路：直接删依赖目录 + 手动把 dependent 保持启用，模拟陈旧状态
        self.market.uninstall("base")
        self.market._enable_in_state("dependent")
        self.market.save()
        # 重新授权（disable 时被清）：隔离出"仅依赖缺失"这一种失败原因
        self.market.grant("dependent", ["skill.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), [])
        reasons = dict(rt.last_report.skipped)
        self.assertIn("依赖不满足", reasons.get("dependent", ""), reasons)


class TestDuplicateInstall(LifecycleCase):
    def test_reinstall_same_id_replaces_and_revokes_trust(self):
        plugin = self.install_reader()
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        self.assertTrue(self.market.find(plugin.id).enabled)

        # 重复安装同 id：目录被替换、代际递增、旧 ack/授权作废
        src2 = write_plugin(self.home / "_src2" / plugin.id, plugin.id, **READER_MANIFEST)
        self.market.install_from_dir(src2)
        again = self.market.find(plugin.id)
        self.assertTrue(again.installed)
        self.assertFalse(again.enabled, "重装后必须回未启用态")
        self.assertTrue(again.needs_ack, "重装后旧确认必须失效")
        # 安装索引仍只有一条记录，目录只有一个
        state = self.market.state()
        self.assertEqual(list(state["installed"]), [plugin.id])
        plugin_dirs = [d.name for d in self.market.plugins_dir.iterdir() if d.is_dir()]
        self.assertEqual(plugin_dirs, [plugin.id])
        # 确认→启用→调用链路在新指纹上完整可走
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        self.market.grant(plugin.id, ["repo.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), ["lifecycle_read"])


class TestConcurrentLoad(LifecycleCase):
    def test_concurrent_reload_and_lifecycle_mutations(self):
        """两个市场实例并发：一个做生命周期变更，一个做目录读+能力加载。"""
        plugin = self.install_reader()
        self.market.ack(plugin.id)
        self.market.enable(plugin.id)
        self.market.grant(plugin.id, ["repo.read"], self.ws)

        other = pm.Marketplace(self.home / "forge")
        errors: list[str] = []
        barrier = threading.Barrier(4)

        def churn_install_uninstall():
            try:
                barrier.wait()
                for i in range(3):
                    src = write_plugin(self.home / f"_churn{i}" / "churn", "churn",
                                       version=f"1.0.{i}", **READER_MANIFEST)
                    other.install_from_dir(src)
                    other.uninstall("churn")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"churn: {exc}")

        def churn_enable_disable():
            try:
                barrier.wait()
                other_market = pm.Marketplace(self.home / "forge")
                for _ in range(3):
                    other_market.disable(plugin.id)
                    other_market.enable(plugin.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"toggle: {exc}")

        def reader_reload():
            try:
                barrier.wait()
                for _ in range(3):
                    rt = pc.CapabilityRuntime(other, self.ws, "concurrent", self.dispatch)
                    rt.reload(reserved_names=self.gateway.names())
                    rt.names()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reload: {exc}")

        def reader_catalog():
            try:
                barrier.wait()
                for _ in range(3):
                    pm.Marketplace(self.home / "forge").catalog()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"catalog: {exc}")

        threads = [threading.Thread(target=fn) for fn in
                   (churn_install_uninstall, churn_enable_disable, reader_reload, reader_catalog)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        # 终态一致：安装索引、启用列表、磁盘目录三者对齐
        final = pm.Marketplace(self.home / "forge")
        state = final.state()
        self.assertEqual(set(state["installed"]) - {"churn"}, {plugin.id})
        on_disk = {d.name for d in final.plugins_dir.iterdir() if d.is_dir()}
        self.assertEqual(on_disk, {plugin.id})
        enabled = set(state["enabled"])
        self.assertEqual(enabled, {plugin.id})


class TestNativePluginRecovery(unittest.TestCase):
    """原生（executes_code）插件：崩溃/超时后的恢复（进程边界隔离）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forge-native-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.market = pm.Marketplace(home=self.home)
        import plugin_runtime as prmod
        self.pr = prmod

    def make_plugin(self, pid: str, body: str):
        src = self.home / "_src" / pid
        src.mkdir(parents=True, exist_ok=True)
        (src / pm.MANIFEST_NAME).write_text(json.dumps({
            "id": pid, "name": pid, "version": "1.0.0", "executes_code": True}),
            encoding="utf-8")
        (src / "plugin.py").write_text(textwrap.dedent(body), encoding="utf-8")
        self.market.install_from_dir(src)
        self.market.ack(pid)
        self.market.enable(pid)

    def test_crash_then_runtime_recovers_for_other_tools(self):
        """崩溃工具返回错误；同一运行时里的健康工具不受影响。"""
        self.make_plugin("boomer", """
            def register():
                def boom(args):
                    raise RuntimeError("插件故意崩溃")
                def fine(args):
                    return {"ok_text": "仍然可用"}
                return [
                    {"name": "boom_tool", "description": "崩溃", "execute": boom},
                    {"name": "fine_tool", "description": "健康", "execute": fine},
                ]
        """)
        rt = self.pr.PluginRuntime(self.market, secret_isolation=False)
        report = rt.reload()
        self.assertEqual(report.loaded, ["boomer"])
        crashed = rt.call("boom_tool", {})
        self.assertFalse(crashed["ok"])
        self.assertIn("插件故意崩溃", crashed["error"])
        healthy = rt.call("fine_tool", {})
        self.assertTrue(healthy["ok"], healthy)
        self.assertEqual(healthy["result"], {"ok_text": "仍然可用"})

    def test_timeout_returns_promptly_and_generation_change_blocks_stale(self):
        """超时被看门狗掐断；超时后禁用插件，陈旧工具立即失效。"""
        self.make_plugin("sleeper", """
            def register():
                import time
                def slow(args):
                    time.sleep(30)
                    return {}
                return [{"name": "slow_tool", "description": "慢", "execute": slow}]
        """)
        rt = self.pr.PluginRuntime(self.market, exec_timeout=1.0, secret_isolation=False)
        rt.reload()
        import time
        started = time.perf_counter()
        out = rt.call("slow_tool", {})
        took = time.perf_counter() - started
        self.assertFalse(out["ok"])
        self.assertIn("超过", out["error"])
        self.assertLess(took, 10, "超时应快速返回")
        # 禁用后代际变化：has() 立即失效，调用被拒
        self.market.disable("sleeper")
        self.assertFalse(rt.has("slow_tool"))
        stale = rt.call("slow_tool", {})
        self.assertFalse(stale["ok"])

    def test_load_failure_isolated_and_reported(self):
        """register 抛错的插件只进报告，不影响目录里的其他插件。"""
        self.make_plugin("broken", """
            def register():
                raise ValueError("加载期失败")
        """)
        self.make_plugin("healthy", """
            def register():
                def ping(args):
                    return {"pong": True}
                return [{"name": "ping_tool", "description": "ping", "execute": ping}]
        """)
        rt = self.pr.PluginRuntime(self.market, secret_isolation=False)
        report = rt.reload()
        self.assertEqual(report.loaded, ["healthy"])
        self.assertEqual(report.errors[0][0], "broken")
        self.assertIn("加载期失败", report.errors[0][1])
        self.assertTrue(rt.call("ping_tool", {})["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
