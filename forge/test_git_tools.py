# -*- coding: utf-8 -*-
"""mediated git 四件套离线单测：真实 git，临时仓库，受保护配置（isolated ctx）。"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from .policy import Mode, Policy
from .tools import ToolContext, build_builtin_registry


def _git(ws, *argv):
    return subprocess.run(["git"] + list(argv), capture_output=True, text=True, cwd=str(ws))


class MediatedGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "t@t")
        _git(self.ws, "config", "user.name", "t")
        (self.ws / "a.txt").write_text("v1", encoding="utf-8")
        self.reg = build_builtin_registry()
        self.ctx = ToolContext(policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS),
                               workspace=self.ws, extras={"registry": self.reg})

    def invoke(self, name, **args):
        return self.reg.invoke(name, args, self.ctx)

    def test_full_workflow(self):
        r = self.invoke("git_status")
        self.assertTrue(r.ok, r.error)
        r = self.invoke("git_add", paths="a.txt")
        self.assertTrue(r.ok, r.error)
        r = self.invoke("git_commit", message="init: v1")
        self.assertTrue(r.ok, r.error)
        r = self.invoke("git_log")
        self.assertTrue(r.ok)
        self.assertIn("init: v1", r.content)

    def test_empty_commit_message(self):
        self.assertFalse(self.invoke("git_commit", message="").ok)

    def test_status_clean(self):
        self.invoke("git_add", paths=".")
        self.invoke("git_commit", message="x")
        r = self.invoke("git_status")
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
