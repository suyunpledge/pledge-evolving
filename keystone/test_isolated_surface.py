"""An isolated (protected) caller must not be offered tools the gate always refuses.

Regression for an agent review (2026-11-25): ``shell_exec`` was listed by the
gateway's tool listing but every call failed with "Native execution is
unavailable in a protected agent".
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from keystone.policy import Mode, Policy, Sandbox
from keystone.tools import ToolContext, build_builtin_registry


class IsolatedSurface(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.reg = build_builtin_registry()
        self.policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=self.ws,
                             non_interactive=True)

    def names(self, isolated):
        ctx = ToolContext(policy=self.policy, workspace=self.ws, extras={"registry": self.reg},
                          isolated=isolated)
        return {spec.name for spec in self.reg.available_specs(ctx)}

    def test_isolated_listing_omits_native_execution(self):
        isolated = self.names(True)
        self.assertNotIn("shell_exec", isolated)
        self.assertNotIn("notebook_edit", isolated)
        # Mediated alternatives stay available.
        self.assertIn("git_status", isolated)
        self.assertIn("read_file", isolated)

    def test_tool_search_explains_unusable_matches(self):
        ctx = ToolContext(policy=self.policy, workspace=self.ws, extras={"registry": self.reg})
        result = self.reg.invoke("tool_search", {"query": "run shell command"}, ctx)
        self.assertTrue(result.ok, result.error)
        unavailable = {row["name"]: row["reason"] for row in result.meta["unavailable"]}
        self.assertIn("shell_exec", unavailable)
        self.assertNotIn("shell_exec", result.meta["activated"])
        chinese = self.reg.invoke("tool_search", {"query": "子任务 分派"}, ctx)
        self.assertIn("spawn_subagent", {r["name"] for r in chinese.meta["unavailable"]})
        self.assertIn("git_commit", self.reg.invoke("tool_search", {"query": "版本 提交"}, ctx).content)
        self.assertIn("电脑操作页", self.reg.invoke("tool_search", {"query": "窗口操作"}, ctx).content)
        missing = self.reg.invoke("tool_search", {"query": "spawn subagent"}, ctx).meta["unavailable"]
        self.assertIn("spawn_subagent", {row["name"] for row in missing})

    def test_trusted_host_still_sees_native_tools(self):
        self.assertNotIn("shell_exec", self.names(False))
        self.policy = replace(self.policy, non_interactive=False)
        self.assertIn("shell_exec", self.names(False))


if __name__ == "__main__":
    unittest.main()
