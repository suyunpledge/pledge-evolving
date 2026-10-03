"""plugin_market 的单元测试。

纪律：**全部在临时 home 下跑**，绝不触碰真实 ~/.forge（会话历史覆盖事故的教训）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import plugin_market as pm  # noqa: E402


def write_manifest(folder: Path, **fields) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"id": folder.name, "name": folder.name, "version": "1.0.0"}
    payload.update(fields)
    (folder / pm.MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return folder


class TempHomeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forge-market-test-")
        self.home = Path(self._tmp.name)
        self.market = pm.Marketplace(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    # ── 隔离 ────────────────────────────────────────────────────────

    def test_never_touches_real_forge_home(self):
        """装/卸/启停全程只写临时目录。"""
        self.market.install_from_catalog("tool-web-search", enable=True)
        root = self.home
        self.assertTrue((root / "marketplace" / "state.json").exists())
        self.assertTrue((root / "plugins" / "tool-web-search").is_dir())
        # 默认构造函数才指向 ~/.forge；这里已显式换掉
        self.assertNotEqual(self.market.home, Path.home() / ".forge")

    # ── 清单解析 ────────────────────────────────────────────────────

    def test_manifest_requires_id(self):
        with self.assertRaises(ValueError):
            pm.parse_manifest({"name": "没有 id"})
        with self.assertRaises(ValueError):
            pm.parse_manifest("不是对象")  # type: ignore[arg-type]

    def test_manifest_unknown_kind_falls_back_to_tool(self):
        p = pm.parse_manifest({"id": "x", "name": "X", "kind": "外星种类"})
        self.assertEqual(p.kind, "tool")
        self.assertEqual(p.kind_label, "工具")

    def test_executes_code_only_accepts_true(self):
        """字符串 "true" / 数字 1 都不算——清单是外部输入，不能被含糊值骗过。"""
        self.assertFalse(pm.parse_manifest({"id": "a", "executes_code": "true"}).executes_code)
        self.assertFalse(pm.parse_manifest({"id": "b", "executes_code": 1}).executes_code)
        self.assertTrue(pm.parse_manifest({"id": "c", "executes_code": True}).executes_code)

    def test_manifest_unknown_fields_are_dropped(self):
        p = pm.parse_manifest({"id": "x", "name": "X", "后门": "rm -rf /"})
        self.assertFalse(hasattr(p, "后门"))
        self.assertNotIn("后门", p.to_dict())

    def test_permissions_render_as_human_text(self):
        p = pm.parse_manifest({"id": "x", "name": "X",
                               "permissions": ["shell:exec", "未登记的权限"]})
        labels = p.permission_labels()
        self.assertIn("执行本机命令", labels)
        self.assertIn("未登记的权限", labels)  # 未知的也照实显示，不吞

    # ── 目录 ────────────────────────────────────────────────────────

    def test_builtin_catalog_reads_from_repo(self):
        entries = self.market.builtin_entries()
        self.assertGreaterEqual(len(entries), 8)
        ids = {e.id for e in entries}
        self.assertIn("theme-wechat-dark", ids)
        self.assertIn("tool-gateway-bridge", ids)
        for e in entries:
            self.assertEqual(e.source, "builtin")
            self.assertTrue(e.summary, f"{e.id} 缺 summary")

    def test_missing_catalog_is_empty_not_crash(self):
        market = pm.Marketplace(home=self.home,
                                builtin_catalog=self.home / "不存在.json")
        self.assertEqual(market.builtin_entries(), [])
        self.assertEqual(market.catalog(), [])

    def test_broken_catalog_is_logged_and_empty(self):
        bad = self.home / "bad.json"
        bad.write_text("{不是 JSON", encoding="utf-8")
        market = pm.Marketplace(home=self.home, builtin_catalog=bad)
        self.assertEqual(market.builtin_entries(), [])
        self.assertTrue(any(r["event"] == "catalog-unreadable"
                            for r in market.log_tail()))

    # ── 安装 / 卸载 ─────────────────────────────────────────────────

    def test_install_from_catalog_then_enable(self):
        p = self.market.install_from_catalog("tool-web-search")
        self.assertTrue(p.installed)
        self.assertFalse(p.enabled, "装完默认不该自动启用")
        p = self.market.enable("tool-web-search")
        self.assertTrue(p.enabled)
        state = self.market.state()
        self.assertIn("tool-web-search", state["enabled"])
        self.assertIn("tool-web-search", state["installed"])

    def test_enable_requires_install(self):
        with self.assertRaises(ValueError):
            self.market.enable("tool-git-inspect")  # 没装过

    def test_disable_is_idempotent(self):
        self.market.install_from_catalog("tool-git-inspect", enable=True)
        self.market.disable("tool-git-inspect")
        self.market.disable("tool-git-inspect")
        state = self.market.state()
        self.assertNotIn("tool-git-inspect", state["enabled"])
        self.assertEqual(state["disabled"].count("tool-git-inspect"), 1)

    def test_executes_code_entry_cannot_install_from_catalog(self):
        """会跑代码的条目必须走本地目录安装，不能一键装。"""
        with self.assertRaises(ValueError) as ctx:
            self.market.install_from_catalog("tool-custom-script")
        self.assertIn("executes_code", str(ctx.exception))

    def test_install_from_dir_copies_and_uninstall_keeps_trash(self):
        src = write_manifest(self.home.parent / "src-plugin",
                             id="my-plugin", name="我的插件",
                             kind="tool", summary="本地插件")
        p = self.market.install_from_dir(src)
        self.assertTrue(p.installed)
        dest = self.home / "plugins" / "my-plugin"
        self.assertTrue((dest / pm.MANIFEST_NAME).exists())
        self.market.uninstall("my-plugin")
        self.assertFalse(dest.exists())
        trash = list((self.home / "marketplace" / "trash").glob("my-plugin.removed-*"))
        self.assertEqual(len(trash), 1, "卸载应保留可恢复副本")

    def test_install_from_dir_rejects_path_traversal_id(self):
        src = write_manifest(self.home.parent / "evil", id="../逃逸", name="坏")
        with self.assertRaises(ValueError):
            self.market.install_from_dir(src)

    def test_install_upgrade_backs_up_old_version(self):
        src = write_manifest(self.home.parent / "up", id="up-plugin",
                             name="升级插件", version="1.0.0")
        self.market.install_from_dir(src)
        src2 = write_manifest(self.home.parent / "up2", id="up-plugin",
                              name="升级插件", version="2.0.0")
        self.market.install_from_dir(src2)
        backups = list((self.home / "marketplace" / "trash").glob("up-plugin.bak-*"))
        self.assertEqual(len(backups), 1)

    # ── ack 信任流程 ────────────────────────────────────────────────

    def test_executes_code_needs_ack_then_enable(self):
        src = write_manifest(self.home.parent / "scripted", id="scripted",
                             name="脚本插件", executes_code=True,
                             permissions=["shell:exec"])
        p = self.market.install_from_dir(src)
        self.assertTrue(p.needs_ack, "会执行代码就该要确认")
        with self.assertRaises(PermissionError):
            self.market.enable("scripted")
        p = self.market.ack("scripted")
        self.assertFalse(p.needs_ack)
        p = self.market.enable("scripted")
        self.assertTrue(p.enabled)

    def test_ack_is_invalidated_when_version_changes(self):
        """指纹随版本/权限变化失效——升级后必须重新确认。"""
        src = write_manifest(self.home.parent / "s1", id="fp", name="指纹",
                             version="1.0.0", executes_code=True)
        self.market.install_from_dir(src)
        self.market.ack("fp")
        self.assertFalse(self.market.find("fp").needs_ack)
        src2 = write_manifest(self.home.parent / "s2", id="fp", name="指纹",
                              version="2.0.0", executes_code=True)
        self.market.install_from_dir(src2)
        self.assertTrue(self.market.find("fp").needs_ack,
                        "版本变了，旧确认应失效")

    def test_revoke_ack_disables_plugin(self):
        src = write_manifest(self.home.parent / "s3", id="rv", name="回收",
                             executes_code=True)
        self.market.install_from_dir(src)
        self.market.ack("rv")
        self.market.enable("rv")
        self.market.revoke_ack("rv")
        p = self.market.find("rv")
        self.assertTrue(p.needs_ack)
        self.assertFalse(p.enabled)

    # ── 状态容错 ────────────────────────────────────────────────────

    def test_corrupt_state_falls_back_to_empty(self):
        market_dir = self.home / "marketplace"
        market_dir.mkdir(parents=True, exist_ok=True)
        (market_dir / "state.json").write_text("{坏掉的", encoding="utf-8")
        market = pm.Marketplace(home=self.home)
        self.assertEqual(market.state()["enabled"], [])
        self.assertTrue(any(r["event"] == "state-unreadable"
                            for r in market.log_tail()))

    def test_state_roundtrip_and_no_alias_leak(self):
        self.market.install_from_catalog("tool-web-search", enable=True)
        snapshot = self.market.state()
        snapshot["enabled"].append("偷偷加的")     # 改快照不该影响内部
        self.assertNotIn("偷偷加的", self.market.state()["enabled"])
        reloaded = pm.Marketplace(home=self.home)
        self.assertIn("tool-web-search", reloaded.state()["enabled"])

    def test_log_records_events(self):
        self.market.install_from_catalog("panel-sysmon", enable=True)
        self.market.disable("panel-sysmon")
        events = [r["event"] for r in self.market.log_tail()]
        self.assertIn("install", events)
        self.assertIn("disable", events)

    # ── 摘要 ────────────────────────────────────────────────────────

    def test_summary_counts(self):
        self.market.install_from_catalog("tool-web-search", enable=True)
        self.market.install_from_catalog("tool-git-inspect")
        s = self.market.summary()
        self.assertGreaterEqual(s["total"], 9)
        self.assertEqual(s["installed"], 2)
        self.assertEqual(s["enabled"], 1)
        self.assertEqual(s["region"], "china")
        self.assertEqual(s["sources"], [])
        self.assertEqual(s["kinds"]["theme"], 2)

    def test_enabled_tools_reflects_only_enabled_plugins(self):
        self.assertEqual(self.market.enabled_tools(), [])
        self.market.install_from_catalog("tool-web-search", enable=True)
        tools = self.market.enabled_tools()
        self.assertIn("web_search", tools)
        self.assertIn("open_link", tools)
        self.assertNotIn("git_status", tools, "未启用插件的工具不该出现")

    def test_render_text_lists_entries(self):
        text = self.market.render_text()
        self.assertIn("微信暗色主题", text)
        self.assertIn("Forge 市场", text)


class ToolCatalogCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="forge-tools-test-")
        self.home = Path(self._tmp.name)
        self.market = pm.Marketplace(home=self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_gateway_prefix_is_stripped_and_deduped(self):
        entries = pm.build_tool_catalog(
            ["forge_read_file", "read_file", "forge_write_file"])
        names = [e.name for e in entries]
        self.assertEqual(names.count("read_file"), 1, "剥前缀后要去重")
        self.assertIn("write_file", names)
        for e in entries:
            self.assertFalse(e.name.startswith("forge_"))

    def test_dangerous_tools_flagged(self):
        entries = {e.name: e for e in pm.build_tool_catalog(
            ["read_file", "write_file", "shell_exec", "list_dir"])}
        self.assertTrue(entries["write_file"].danger)
        self.assertTrue(entries["shell_exec"].danger)
        self.assertFalse(entries["read_file"].danger)
        self.assertFalse(entries["list_dir"].danger)

    def test_gateway_none_still_returns_plugin_tools(self):
        """桥不可用也不能整表空——插件工具照给。"""
        self.market.install_from_catalog("tool-web-search", enable=True)
        entries = pm.build_tool_catalog(None, self.market)
        names = [e.name for e in entries]
        self.assertIn("web_search", names)
        self.assertTrue(all(e.source == "plugin" for e in entries))

    def test_gateway_wins_over_plugin_same_name(self):
        self.market.install_from_catalog("tool-web-search", enable=True)
        entries = pm.build_tool_catalog(["web_search"], self.market)
        matched = [e for e in entries if e.name == "web_search"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].source, "gateway")

    def test_disabled_plugin_tools_are_hidden(self):
        self.market.install_from_catalog("tool-web-search")   # 只装不启
        entries = pm.build_tool_catalog([], self.market)
        self.assertEqual([e.name for e in entries], [])


class DefaultHomeCase(unittest.TestCase):
    """只验证默认路径解析，不写盘。"""

    def test_default_marketplace_points_at_forge_home(self):
        market = pm.default_marketplace()
        self.assertEqual(market.home, Path.home() / ".forge")
        self.assertEqual(market.state_path.name, "state.json")
        self.assertEqual(market.log_path.name, "log.ndjson")


if __name__ == "__main__":
    unittest.main(verbosity=2)
