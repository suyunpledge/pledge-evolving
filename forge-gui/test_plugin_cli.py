"""test_plugin_cli.py — headless 生命周期 CLI、doctor 诊断与 compat 实体复制。

验证不开 GUI 也能完成 install→ack→enable→grant→调用；外部 OpenClaw 技能
条目安装时真实复制 SKILL.md 并可被受控加载。全部使用临时目录。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_market as pm
import compat_sources as cs


def make_skill_source(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


class HeadlessCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "forge"
        self.ws = Path(self.tmp.name) / "repo"
        self.ws.mkdir()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = pm.main(list(argv))
        return code, out.getvalue()

    def test_full_headless_lifecycle_with_builtin_skill(self):
        code, _ = self.run_cli("install", "skill-forge-guide", "--home", str(self.home))
        self.assertEqual(code, 0)
        market = pm.Marketplace(self.home)
        plugin = market.find("skill-forge-guide")
        self.assertTrue(plugin.installed)
        self.assertFalse(plugin.enabled)
        self.assertTrue(plugin.needs_ack)
        # headless ack → enable → grant
        for argv in (("ack", "skill-forge-guide"), ("enable", "skill-forge-guide")):
            code, _ = self.run_cli(*argv, "--home", str(self.home))
            self.assertEqual(code, 0)
        code, out = self.run_cli("grant", "skill-forge-guide", "--home", str(self.home),
                                 "--workspace", str(self.ws),
                                 "--capability", "skill.read")
        self.assertEqual(code, 0)
        # 真实调用：正文来自内联 provides.skill_body
        import plugin_capabilities as pc
        rt = pc.CapabilityRuntime(market, self.ws, "cli-test", None)
        rt.reload()
        self.assertEqual(rt.names(), ["forge_skill_guide"])
        result = rt.call("forge_skill_guide", {})
        self.assertTrue(result["ok"], result)
        self.assertIn("声明式技能正文", result["content"])
        self.assertIn("forge_skill_guide", result["content"])

    def test_doctor_reports_orphan_and_dependency_problems(self):
        self.run_cli("install", "skill-forge-guide", "--home", str(self.home))
        # 人为制造孤儿目录
        orphan = pm.Marketplace(self.home).plugins_dir / "stray-dir"
        orphan.mkdir(parents=True)
        (orphan / "junk.txt").write_text("x", encoding="utf-8")
        # 依赖装好后被卸载：启用态插件缺依赖，doctor 必须报出
        base_src = Path(self.tmp.name) / "base"
        base_src.mkdir()
        (base_src / pm.MANIFEST_NAME).write_text(json.dumps({
            "id": "base", "name": "base", "version": "1.0.0"}), encoding="utf-8")
        dep_src = Path(self.tmp.name) / "dependent"
        dep_src.mkdir()
        (dep_src / pm.MANIFEST_NAME).write_text(json.dumps({
            "id": "dependent", "name": "dependent", "version": "1.0.0",
            "requires": {"plugins": {"base": ">=1.0"}}}), encoding="utf-8")
        market = pm.Marketplace(self.home)
        market.install_from_dir(base_src)
        market.install_from_dir(dep_src)
        market.ack("dependent")
        market.enable("dependent")
        market.uninstall("base")
        report, healthy = pm.doctor_report(market, workspace=self.ws)
        self.assertFalse(healthy)
        self.assertIn("stray-dir", report)
        self.assertIn("依赖的插件未安装：base", report)

    def test_updates_and_audit_subcommands(self):
        code, out = self.run_cli("updates", "--home", str(self.home))
        self.assertEqual(code, 0)
        self.assertIn("没有可用更新", out)
        code, _ = self.run_cli("audit", "--home", str(self.home))
        self.assertEqual(code, 0)

    def test_disable_and_uninstall_via_cli(self):
        self.run_cli("install", "skill-forge-guide", "--home", str(self.home))
        self.run_cli("ack", "skill-forge-guide", "--home", str(self.home))
        self.run_cli("enable", "skill-forge-guide", "--home", str(self.home))
        code, _ = self.run_cli("disable", "skill-forge-guide", "--home", str(self.home))
        self.assertEqual(code, 0)
        code, _ = self.run_cli("uninstall", "skill-forge-guide", "--home", str(self.home))
        self.assertEqual(code, 0)
        market = pm.Marketplace(self.home)
        self.assertFalse(market.find("skill-forge-guide").installed)


class CompatBodyCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "repo"
        self.ws.mkdir()
        self.skill_root = self.root / "openclaw" / "skills"
        make_skill_source(self.skill_root, "review-helper",
                           "---\nname: review\ndescription: 代码评审助手\n---\n\n# 评审流程\n逐文件检查。")

    def test_openclaw_skill_installs_body_and_executes(self):
        with patch.object(cs, "OPENCLAW_SKILL_ROOTS", [self.skill_root]):
            cs._SCAN_CACHE.clear()
            cs._SCAN_CACHE_AT.clear()
            market = pm.Marketplace(self.root / "forge", external_sources=True)
            entries = market.external_entries()
            pid = "openclaw-review-helper"
            self.assertIn(pid, [p.id for p in entries])
            market.install_from_catalog(pid)
            plugin = market.find(pid)
            self.assertTrue(plugin.installed)
            # SKILL.md 真实复制进了插件目录（不再是纯清单摆设）
            body_file = Path(plugin.path) / "SKILL.md"
            self.assertTrue(body_file.is_file())
            self.assertIn("评审流程", body_file.read_text(encoding="utf-8"))
            # 生命周期 + 真实调用
            market.ack(pid, expected_fingerprint=plugin.ack_of())
            market.enable(pid)
            market.grant(pid, ["skill.read"], self.ws)
            import plugin_capabilities as pc
            rt = pc.CapabilityRuntime(market, self.ws, "compat-test", None)
            rt.reload()
            self.assertEqual(rt.names(), ["openclaw_skill_review-helper"])
            result = rt.call("openclaw_skill_review-helper", {})
            self.assertTrue(result["ok"], result)
            self.assertIn("逐文件检查", result["content"])

    def test_remote_entries_never_gain_install_dir(self):
        # 远程 payload 无 install_dir：_compat_body_files 必须返回空
        market = pm.Marketplace(self.root / "forge", external_sources=True)
        fake = pm.parse_manifest({
            "id": "openclaw-remote-thing", "name": "x", "version": "1.0.0",
            "capabilities": ["skill.read"],
            "contributions": {"skills": [{"name": "x_skill", "file": "SKILL.md"}]}},
            source="remote")
        with patch.object(cs, "OPENCLAW_SKILL_ROOTS", [self.skill_root]):
            cs._SCAN_CACHE.clear()
            cs._SCAN_CACHE_AT.clear()
            # 同 id 的本机条目不存在 → 无可复制实体
            files = market._compat_body_files(fake)
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
