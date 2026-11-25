"""test_plugin_model3d.py — 内置 3D 建模技能插件走完整的声明式插件生命周期。

install → ack → enable → grant skill.read → Agent 侧加载正文；正文与内核
revon/bundled/model3d/SKILL.md 一致，且几何工具由内核注册（插件不执行代码）。
"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_market as pm

PLUGIN = "skill-model3d"


class Model3dSkillPluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "revon"
        self.ws = Path(self.tmp.name) / "repo"
        self.ws.mkdir()

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = pm.main([*argv, "--home", str(self.home)])
        return code, out.getvalue()

    def test_catalog_entry_is_declarative(self):
        plugin = pm.Marketplace(self.home).find(PLUGIN)
        self.assertIsNotNone(plugin)
        self.assertFalse(plugin.executes_code)
        self.assertIn("skill.read", plugin.declared_capabilities)

    def test_lifecycle_loads_playbook_for_the_agent(self):
        self.assertEqual(self.cli("install", PLUGIN)[0], 0)
        for action in ("ack", "enable"):
            self.assertEqual(self.cli(action, PLUGIN)[0], 0)
        code, _ = self.cli("grant", PLUGIN, "--workspace", str(self.ws), "--capability", "skill.read")
        self.assertEqual(code, 0)
        import plugin_capabilities as pc
        runtime = pc.CapabilityRuntime(pm.Marketplace(self.home), self.ws, "model3d-test", None)
        report = runtime.reload()
        self.assertIn(PLUGIN, report.loaded, report.errors)
        self.assertEqual(runtime.names(), ["keystone_model3d_guide"])
        result = runtime.call("keystone_model3d_guide", {})
        self.assertTrue(result["ok"], result)
        self.assertIn("model3d_build", result["content"])
        self.assertIn("plan → build → check → export", result["content"])
        # the geometry itself comes from host tools, which a plugin can never shadow
        from plugin_runtime import BUILTIN_TOOL_NAMES
        self.assertTrue({"model3d_build", "model3d_inspect"} <= BUILTIN_TOOL_NAMES)

    def test_disable_revokes_the_skill(self):
        self.cli("install", PLUGIN)
        self.cli("ack", PLUGIN)
        self.cli("enable", PLUGIN)
        self.cli("grant", PLUGIN, "--workspace", str(self.ws), "--capability", "skill.read")
        import plugin_capabilities as pc
        runtime = pc.CapabilityRuntime(pm.Marketplace(self.home), self.ws, "model3d-test", None)
        runtime.reload()
        self.assertEqual(self.cli("disable", PLUGIN)[0], 0)
        self.assertEqual(runtime.names(), [])
        self.assertFalse(runtime.call("keystone_model3d_guide", {})["ok"])


if __name__ == "__main__":
    unittest.main()
