"""test_plugin_skills.py — contributions.skills 受控执行器回归。

真实调用验证：知悉→启用→授予 skill.read 后 rt.call() 返回技能正文；
dispatch 间谍确认技能调用完全不经网关。所有夹具使用临时 home，互不污染。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugin_market as pm
import plugin_capabilities as pc
from forge.tools import ToolContext, build_builtin_registry
from forge.policy import Mode, Policy


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws = self.home / "repo"
        self.ws.mkdir()
        (self.ws / "hello.txt").write_text("hello", encoding="utf-8")
        self.market = pm.Marketplace(self.home / "forge")
        self.gateway = build_builtin_registry()
        self.policy = Policy(mode=Mode.READ_ONLY, workspace=self.ws)
        self.dispatch = Mock(side_effect=self._real_dispatch)

    def _real_dispatch(self, name, args, **kwargs):
        return self.gateway.invoke(name.removeprefix("forge_"), args,
            ToolContext(self.policy, self.ws)).as_dict()

    def install_skill_plugin(self, pid="skiller", *, body="# 技能正文\n\n步骤一…",
                             inline=False, capability="skill.read", **fields):
        src = self.home / pid
        src.mkdir(exist_ok=True)
        entry = {"name": pid + "_skill", "description": "加载技能正文"}
        if not inline:
            entry["file"] = "SKILL.md"
            (src / "SKILL.md").write_text(body, encoding="utf-8")
        raw = {"id": pid, "name": pid, "capabilities": [capability],
               "contributions": {"skills": [entry]}, **fields}
        if inline:
            raw["provides"] = {"tools": [], "skill_body": body}
        (src / pm.MANIFEST_NAME).write_text(json.dumps(raw, ensure_ascii=False),
                                            encoding="utf-8")
        return self.market.install_from_dir(src)

    def ready(self, **kwargs):
        p = self.install_skill_plugin(**kwargs)
        self.market.ack(p.id, expected_fingerprint=p.ack_of())
        self.market.enable(p.id)
        return self.market.grant(p.id, [kwargs.get("capability", "skill.read")], self.ws)

    def runtime(self, session="session-1"):
        rt = pc.CapabilityRuntime(self.market, self.ws, session, self.dispatch)
        rt.reload(reserved_names=self.gateway.names())
        return rt

    def test_full_lifecycle_real_invocation_file_based(self):
        self.ready()
        rt = self.runtime()
        self.assertEqual(rt.names(), ["skiller_skill"])
        result = rt.call("skiller_skill", {})
        self.assertTrue(result["ok"], result)
        self.assertIn("技能正文", result["content"])
        # 技能调用不碰网关、不碰工作区
        self.dispatch.assert_not_called()

    def test_inline_body_from_builtin_style_manifest(self):
        self.ready(inline=True)
        rt = self.runtime()
        result = rt.call("skiller_skill", {})
        self.assertTrue(result["ok"])
        self.assertIn("技能正文", result["content"])
        self.dispatch.assert_not_called()

    def test_no_grant_no_callable_but_still_discoverable(self):
        p = self.install_skill_plugin()
        self.market.ack(p.id)
        self.market.enable(p.id)
        rt = self.runtime()
        # 装了、知悉了、启用了，但没授权——不可调用，也不出现在模型 schema 里
        self.assertEqual(rt.names(), [])
        self.assertEqual(rt.openai_schemas(), [])
        result = rt.call("skiller_skill", {})
        self.assertFalse(result["ok"])
        self.dispatch.assert_not_called()

    def test_revoke_kills_tool_and_audit_records(self):
        self.ready()
        rt = self.runtime()
        self.assertTrue(rt.call("skiller_skill", {})["ok"])
        self.market.revoke_grants("skiller", workspace=self.ws)
        self.assertFalse(rt.call("skiller_skill", {})["ok"])
        rows = self.market.audit_tail(limit=50)
        self.assertTrue(any(r.get("phase") == "dispatch" and r.get("tool") == "skiller_skill"
                            for r in rows))

    def test_file_change_invalidates_skill(self):
        p = self.install_skill_plugin()
        self.market.ack(p.id, expected_fingerprint=p.ack_of())
        self.market.enable(p.id)
        self.market.grant(p.id, ["skill.read"], self.ws)
        rt = self.runtime()
        self.assertTrue(rt.call("skiller_skill", {})["ok"])
        (self.market.plugins_dir / "skiller" / "SKILL.md").write_text(
            "# 被替换的正文", encoding="utf-8")
        rt2 = self.runtime()
        self.assertEqual(rt2.names(), [])
        self.assertFalse(rt2.call("skiller_skill", {})["ok"])

    def test_undeclared_capability_rejects_group(self):
        p = self.install_skill_plugin(capability="repo.read")  # 声明了别的能力
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.market.grant(p.id, ["repo.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), [])
        self.assertTrue(any(pid == "skiller" for pid, _ in rt.last_report.errors))

    def test_skill_arguments_rejected(self):
        self.ready()
        rt = self.runtime()
        result = rt.call("skiller_skill", {"path": "hello.txt"})
        self.assertFalse(result["ok"])
        self.dispatch.assert_not_called()

    def test_path_escape_in_skill_file_rejected(self):
        src = self.home / "escaper"
        src.mkdir()
        raw = {"id": "escaper", "name": "escaper", "capabilities": ["skill.read"],
               "contributions": {"skills": [
                   {"name": "esc_skill", "file": "../outside.md", "description": "x"}]}}
        (src / pm.MANIFEST_NAME).write_text(json.dumps(raw), encoding="utf-8")
        self.market.install_from_dir(src)
        p = self.market.find("escaper")
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.market.grant(p.id, ["skill.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), [])
        self.assertTrue(rt.last_report.errors)

    def test_skill_and_tool_name_collision_in_one_plugin(self):
        src = self.home / "clash"
        src.mkdir()
        (src / "SKILL.md").write_text("body", encoding="utf-8")
        raw = {"id": "clash", "name": "clash", "capabilities": ["skill.read", "repo.read"],
               "contributions": {
                   "tools": [{"name": "clash_tool", "target": "read_file",
                              "description": "t", "parameters": {
                                  "type": "object", "properties": {"path": {"type": "string"}},
                                  "required": ["path"], "additionalProperties": False}}],
                   "skills": [{"name": "clash_tool", "file": "SKILL.md",
                               "description": "同名技能"}]}}
        (src / pm.MANIFEST_NAME).write_text(json.dumps(raw), encoding="utf-8")
        self.market.install_from_dir(src)
        p = self.market.find("clash")
        self.market.ack(p.id)
        self.market.enable(p.id)
        self.market.grant(p.id, ["skill.read", "repo.read"], self.ws)
        rt = self.runtime()
        self.assertEqual(rt.names(), [])
        self.assertTrue(rt.last_report.errors)

    def test_openai_schema_carries_honest_note(self):
        self.ready()
        schemas = self.runtime().openai_schemas()
        self.assertEqual(len(schemas), 1)
        self.assertIn("不访问工作区", schemas[0]["function"]["description"])
        self.assertEqual(schemas[0]["function"]["parameters"]["properties"], {})


if __name__ == "__main__":
    unittest.main()
