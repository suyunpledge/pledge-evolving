# -*- coding: utf-8 -*-
"""同工具连发导航（窗口化）：滑动窗口占比/每run预算/换工具后新churn可再触发。"""
import unittest

from .loop import Agent, LoopLimits
from .policy import Mode, Policy
from .test_project_rules import Router
from .tools import build_builtin_registry

from pathlib import Path
import tempfile


def fire(seq, agent):
    for t in seq:
        agent._observe_tool_streak(t)
    return bool(agent._streak_pending)


class StreakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        ws = root / "ws"
        ws.mkdir()
        self.agent = Agent(home=root / "home", workspace=ws, router=Router([]),
                           registry=build_builtin_registry(),
                           policy=Policy(workspace=ws, mode=Mode.ACCEPT_EDITS),
                           limits=LoopLimits(max_steps=8))

    def test_continuous_streak_fires_at_six(self):
        self.assertFalse(fire(["grep"] * 5, self.agent))
        self.assertFalse(self.agent._streak_pending)
        self.agent._observe_tool_streak("grep")  # 第 6 次
        self.assertEqual(len(self.agent._streak_pending), 1)
        self.assertIn("python", self.agent._streak_pending[0])

    def test_interleaved_churn_fires_when_window_dominates(self):
        # 3×(grep,artifact_read) + 3×grep = 9 次：窗口(2..9)=a,g,a,g,g,g → grep 5 不触发
        self.assertFalse(fire(["grep", "artifact_read"] * 3 + ["grep"] * 3, self.agent))
        # 第 10 次 grep：窗口(3..10)=g,a,g,g,g,g? 调用3=g,4=a,5=g,6=a?——重算：
        # 调用序列 1g 2a 3g 4a 5g 6a 7g 8g 9g 10g；窗口=3..10=g,a,g,g?（4=a,5=g,6=a→
        # 窗口3..10 = g,a,g,a,g,g,g,g）grep=6 → 触发
        self.agent._observe_tool_streak("grep")
        self.assertTrue(self.agent._streak_pending)

    def test_new_churn_after_tool_switch_fires_again(self):
        fire(["grep"] * 6, self.agent)          # 触发#1
        self.assertTrue(self.agent._streak_pending)
        self.agent._streak_pending = []
        fire(["read_file"] * 8, self.agent)     # 换工具的新 churn——应可再触发
        self.assertTrue(self.agent._streak_pending)
        self.assertIn("python", self.agent._streak_pending[0])

    def test_budget_two_per_run(self):
        fire(["grep"] * 6, self.agent)
        self.agent._streak_pending = []
        fire(["read_file"] * 8, self.agent)
        self.agent._streak_pending = []
        self.assertEqual(self.agent._streak_budget, 0)
        self.agent._tool_window.clear()
        fire(["calculator"] * 8, self.agent)    # 预算耗尽
        self.assertFalse(self.agent._streak_pending)


if __name__ == "__main__":
    unittest.main()
