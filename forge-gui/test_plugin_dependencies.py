"""test_plugin_dependencies.py — requires 校验、版本冲突、更新与恢复回归。

覆盖：约束解析、安装/启用闸门、依赖缺失/降级/环、更新撤销知悉授权、
旧 state.json 跨版本兼容。全部使用临时 home。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_dependencies as pdep
import plugin_market as pm


def write_plugin(src: Path, pid: str, *, version="1.0.0", requires=None,
                 capabilities=None, **extra):
    src.mkdir(parents=True, exist_ok=True)
    raw = {"id": pid, "name": pid, "version": version}
    if requires is not None:
        raw["requires"] = requires
    if capabilities is not None:
        raw["capabilities"] = capabilities
        raw["contributions"] = {"skills": [
            {"name": pid + "_skill", "description": "d",
             "file": "SKILL.md"}]}
        (src / "SKILL.md").write_text("# body", encoding="utf-8")
    raw.update(extra)
    (src / pm.MANIFEST_NAME).write_text(json.dumps(raw, ensure_ascii=False),
                                        encoding="utf-8")
    return src


class VersionConstraintTests(unittest.TestCase):
    def test_parse_and_satisfies(self):
        self.assertTrue(pdep.satisfies("0.8.0", ">=0.7.0"))
        self.assertTrue(pdep.satisfies("0.8.0", ">=0.7"))
        self.assertTrue(pdep.satisfies("1.2.3", "<2.0, !=1.2.4"))
        self.assertFalse(pdep.satisfies("1.2.4", "<2.0, !=1.2.4"))
        self.assertTrue(pdep.satisfies("0.7.0", "0.7.0"))   # 裸版本=精确匹配
        self.assertTrue(pdep.satisfies("1.0", "==1.0.0"))   # 缺段补零

    def test_unsupported_syntax_fails_closed(self):
        for bad in ("^1.0", "~2.0", "*"):
            with self.assertRaises(pdep.RequirementError):
                pdep.parse_constraint(bad)

    def test_garbage_rejected(self):
        for bad in ("", "  ", "abc..d", ">=1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0"):
            with self.assertRaises(pdep.RequirementError):
                pdep.parse_version(bad)
        with self.assertRaises(pdep.RequirementError):
            pdep.normalize_requires({"plugins": {"x": 1}})


class RequirementGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws = self.home / "repo"
        self.ws.mkdir()
        self.market = pm.Marketplace(self.home / "forge")

    def install(self, pid, **kwargs):
        return self.market.install_from_dir(write_plugin(self.home / pid, pid, **kwargs))

    def test_forge_version_too_old_blocks_install(self):
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.install("ancient", requires={"forge": ">=99.0.0"})
        self.assertIn("Forge", str(ctx.exception))

    def test_missing_plugin_dependency_blocks_install(self):
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.install("dependent", requires={"plugins": {"base": ">=1.0"}})
        self.assertIn("未安装", str(ctx.exception))

    def test_dependency_version_conflict_blocks_install(self):
        self.install("base", version="0.9.0", capabilities=["skill.read"])
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.install("dependent", requires={"plugins": {"base": ">=1.0"}})
        self.assertIn("不满足", str(ctx.exception))

    def test_satisfied_dependency_installs_and_enable_rechecks(self):
        base = self.install("base", version="1.5.0", capabilities=["skill.read"])
        self.market.ack("base")
        self.market.enable("base")
        dep = self.install("dependent", requires={"plugins": {"base": ">=1.0"}})
        self.market.ack("dependent")
        self.market.enable("dependent")
        self.assertTrue(self.market.find("dependent").enabled)
        # 卸载依赖后，重新启用被闸门拦下
        self.market.uninstall("base")
        self.market.disable("dependent")
        self.market.ack("dependent")
        with self.assertRaises(pdep.RequirementError):
            self.market.enable("dependent")

    def test_dependency_cycle_detected(self):
        self.install("a1", version="1.0.0", capabilities=["skill.read"])
        self.install("b1", version="1.0.0",
                     requires={"plugins": {"a1": ">=1.0"}}, capabilities=["skill.read"])
        # 把 a1 更新成依赖 b1：a1→b1→a1 闭环，闸门必须在安装时就拦下
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.install("a1", version="1.1.0",
                         requires={"plugins": {"b1": ">=1.0"}},
                         capabilities=["skill.read"])
        self.assertIn("依赖环", str(ctx.exception))
        # a1 仍停留在 1.0.0（失败安装不留半成品）
        self.assertEqual(self.market.find("a1").version, "1.0.0")

    def test_self_dependency_is_a_cycle(self):
        with self.assertRaises(pdep.RequirementError) as ctx:
            self.install("selfish", requires={"plugins": {"selfish": ">=1.0"}})
        self.assertIn("依赖自身", str(ctx.exception))

    def test_unknown_requires_keys_tolerated_but_reported(self):
        plugin = self.install("legacy", requires={"dsh": "bundled"})
        self.assertEqual(plugin.requires, {"dsh": "bundled"})  # 不硬拒
        self.assertEqual(pdep.normalize_requires(plugin.requires)["unknown"], ["dsh"])


class UpdateAndStateCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws = self.home / "repo"
        self.ws.mkdir()
        self.market = pm.Marketplace(self.home / "forge")

    def test_update_over_install_revokes_ack_and_grants(self):
        write_plugin(self.home / "p", "p", version="1.0.0", capabilities=["skill.read"])
        self.market.install_from_dir(self.home / "p")
        p = self.market.find("p")
        self.market.ack("p", expected_fingerprint=p.ack_of())
        self.market.enable("p")
        self.market.grant("p", ["skill.read"], self.ws)
        self.assertTrue(self.market.find("p").grants)
        # 更新到 2.0.0（重装目录）
        write_plugin(self.home / "p", "p", version="2.0.0", capabilities=["skill.read"])
        self.market.install_from_dir(self.home / "p")
        updated = self.market.find("p")
        self.assertEqual(updated.version, "2.0.0")
        self.assertFalse(updated.enabled, "更新后必须回到未启用态")
        self.assertTrue(updated.needs_ack, "更新后必须重新知悉")
        self.assertFalse(updated.grants, "更新后授权必须清空")

    def test_check_updates_compares_catalog_versions(self):
        write_plugin(self.home / "p", "p", version="1.0.0", capabilities=["skill.read"])
        self.market.install_from_dir(self.home / "p")
        builtin = json.loads((pm._runtime_base() / "catalog.builtin.json")
                             .read_text(encoding="utf-8"))
        self.assertIn("skill-forge-guide", [e["id"] for e in builtin["entries"]])
        # 已装条目没有目录新版 → 无更新
        updates = self.market.check_updates()
        self.assertEqual([u for u in updates if u["id"] == "p"], [])

    def test_old_state_file_without_new_keys_still_loads(self):
        self.market.market_dir.mkdir(parents=True, exist_ok=True)
        # 模拟旧版本 state：缺 last_sync/grants/generations 等新键
        legacy = {"enabled": ["p"], "installed": {"p": {"version": "0.1.0"}},
                  "acked": {}, "sources": [], "region": "china"}
        (self.market.market_dir / "state.json").write_text(
            json.dumps(legacy), encoding="utf-8")
        state = pm.Marketplace(self.home / "forge").state()
        self.assertEqual(state["region"], "china")
        # enabled 指向不存在的目录 → catalog 自愈摘除
        self.assertEqual(state["enabled"], [])
        self.assertEqual(state["grants"], {})


if __name__ == "__main__":
    unittest.main()
