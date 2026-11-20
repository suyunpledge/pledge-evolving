# -*- coding: utf-8 -*-
"""file_export 离线单测：CSV/JSON 导出、字段序、合并多文件、数值化。"""
import json
import tempfile
import unittest
from pathlib import Path

from .policy import Mode, Policy
from .tools import ToolContext, build_builtin_registry


class FileExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "a.txt").write_text(
            "[r1] g=1 w=2.5 o=甲\n[r2] g=2 w=3 o=乙\n", encoding="utf-8")
        (self.ws / "b.txt").write_text(
            "[r3] g=1 w=4 o=甲\n", encoding="utf-8")
        self.reg = build_builtin_registry()
        self.ctx = ToolContext(policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS),
                               workspace=self.ws, extras={"registry": self.reg})

    def invoke(self, **args):
        return self.reg.invoke("file_export", args, self.ctx)

    def test_csv_merge_and_columns(self):
        r = self.invoke(path="a.txt,b.txt", out="all.csv", format="csv",
                        fields="g,w,o")
        self.assertTrue(r.ok, r.error)
        content = (self.ws / "all.csv").read_text(encoding="utf-8")
        lines = [l for l in content.strip().splitlines() if l]
        self.assertEqual(lines[0], "g,w,o")
        self.assertEqual(len(lines), 4)  # header + 3
        self.assertIn("exported 3 records", r.content)

    def test_bracket_header_becomes_field(self):
        # [module_0001] 风格：方括号头映射为前缀名（module=module_0001）
        r = self.invoke(path="a.txt", out="h.csv", format="csv",
                        fields="id,g,w")
        self.assertTrue(r.ok, r.error)
        content = (self.ws / "h.csv").read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        self.assertEqual(lines[0], "id,g,w")
        self.assertTrue(lines[1].startswith("r1,"), lines[1])
        # 前缀名映射：a.txt 的头是 [r1]，前缀 r 不在 fields 请求里，
        # 用第二组夹具验证 module 前缀
        (self.ws / "m.txt").write_text(
            "[module_0007] g=1 w=9 o=丙\n", encoding="utf-8")
        r2 = self.invoke(path="m.txt", out="m.csv", format="csv",
                         fields="module,g")
        self.assertTrue(r2.ok, r2.error)
        c2 = (self.ws / "m.csv").read_text(encoding="utf-8")
        self.assertIn("module_0007", c2)

    def test_json_numeric_conversion(self):
        r = self.invoke(path="a.txt", out="o.json", format="json", fields="g,w,o")
        self.assertTrue(r.ok)
        data = json.loads((self.ws / "o.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["w"], 2.5)
        self.assertEqual(data[0]["g"], 1)  # 数值化 int

    def test_errors(self):
        self.assertFalse(self.invoke(path="nope.txt", out="x.csv").ok)
        r = self.invoke(path="a.txt", out="x.weird", format="yaml")
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
