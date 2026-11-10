# -*- coding: utf-8 -*-
"""file_stats 聚合工具：解析/过滤/运算/错误路径。"""
import tempfile
import unittest
from pathlib import Path

from .policy import Mode, Policy
from .tools import ToolContext, build_builtin_registry


class FileStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        data = "\n".join(
            f"[m_{i}] group={i % 3} weight={i * 1.5:.1f} owner={'甲' if i % 2 else '乙'}"
            for i in range(1, 11))
        (self.ws / "d.txt").write_text("# 头\n" + data + "\n", encoding="utf-8")
        self.reg = build_builtin_registry()
        self.ctx = ToolContext(policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS),
                               workspace=self.ws,
                               extras={"registry": self.reg})

    def invoke(self, **args):
        return self.reg.invoke("file_stats", args, self.ctx)

    def test_sum_with_filter(self):
        r = self.invoke(path="d.txt", field="weight", ops="sum,count",
                        filters="owner=甲")
        self.assertTrue(r.ok, r.error)
        # i=1,3,5,7,9 → weights 1.5,4.5,7.5,10.5,13.5 → sum=37.5 count=5
        self.assertIn("sum=37.5", r.content)
        self.assertIn("count=5", r.content)

    def test_all_ops_no_filter(self):
        r = self.invoke(path="d.txt", field="weight", ops="sum,count,avg,min,max")
        self.assertTrue(r.ok)
        self.assertIn("min=1.5", r.content)
        self.assertIn("max=15.0", r.content)
        self.assertIn("count=10", r.content)

    def test_numeric_filter_on_group(self):
        r = self.invoke(path="d.txt", field="weight", filters="group=2")
        self.assertTrue(r.ok)
        # i=2,5,8 → 3.0,7.5,12.0 → sum=22.5
        self.assertIn("sum=22.5", r.content)

    def test_multi_file_aggregate(self):
        (self.ws / "e.txt").write_text(
            "\n".join(f"[n_{i}] group={i % 3} weight={i * 2.0:.1f} owner=甲" for i in range(1, 5)),
            encoding="utf-8")
        r = self.invoke(path="d.txt,e.txt", field="weight", filters="owner=甲",
                        ops="sum,count")
        self.assertTrue(r.ok, r.error)
        # d.txt 甲: 1.5+4.5+7.5+10.5+13.5=37.5(5) e.txt 全甲: 2+4+6+8=20(4) → 57.5(9)
        self.assertIn("sum=57.5", r.content)
        self.assertIn("count=9", r.content)
        self.assertIn("2 files", r.content)

    def test_errors(self):
        self.assertFalse(self.invoke(path="missing.txt", field="w").ok)
        self.assertFalse(self.invoke(path="d.txt", field="").ok)
        self.assertFalse(self.invoke(path="d.txt", field="weight", ops="median").ok)

    def test_no_match_reports_zero(self):
        r = self.invoke(path="d.txt", field="weight", filters="owner=丙")
        self.assertTrue(r.ok)
        self.assertIn("matched", r.content)


if __name__ == "__main__":
    unittest.main()
