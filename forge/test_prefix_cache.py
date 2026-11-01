"""前缀缓存：主循环请求在一次运行内只追加；项目规则跨轮位置固定。

DeepSeek / OpenAI 按请求前缀逐字节命中缓存。这里用录制型假模型驱动真实
build_agent 主循环，直接比对相邻请求的消息列表。
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from forge.config import load_config
from forge.loop import LoopLimits, build_agent
from forge.memory import MemoryStore
from forge.model import ModelRouter, Provider, Usage
from forge.policy import Mode, Policy, Sandbox


def _call(tool, **args):
    return "<tool_call>" + json.dumps({"tool": tool, "args": args}, ensure_ascii=False) + "</tool_call>"


class _Recorder:
    def __init__(self):
        self.calls, self.script = [], []

    def complete(self, provider, model, messages, **options):
        self.calls.append({"tools": copy.deepcopy(options.get("tools")),
                           "messages": copy.deepcopy(messages)})
        reply = self.script.pop(0) if self.script else "ok"
        return reply, Usage(prompt_tokens=100, completion_tokens=10, model=model, provider=provider.name)


class PrefixCacheTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._ws = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._home.cleanup)
        self.addCleanup(self._ws.cleanup)
        home, ws = Path(self._home.name), Path(self._ws.name)
        (ws / "src").mkdir()
        (ws / "AGENTS.md").write_text("- 改代码后补测试。\n", encoding="utf-8")
        (ws / "src" / "AGENTS.md").write_text("- src 下的函数都要有类型标注。\n", encoding="utf-8")
        (ws / "src" / "app.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
        (ws / "README.md").write_text("# demo\n", encoding="utf-8")
        store = MemoryStore(home / "MEMORY.md").load()
        store.remember("用户偏好简洁回答", kind="user", source="human")
        store.save()
        self.rec = _Recorder()
        router = ModelRouter([Provider(name="main", base_url="http://x")], transport=self.rec,
                             chain=[("main", "deepseek-flash")], retries_per_provider=0)
        policy = Policy(mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, workspace=ws,
                        non_interactive=True)
        self.agent = build_agent(home=home, workspace=ws, config=load_config(home), router=router,
                                 policy=policy, limits=LoopLimits(max_steps=8, max_depth=1, spawn_budget=0))
        self.addCleanup(lambda: self.agent.session.close() if getattr(self.agent, "session", None) else None)

    def _rules_index(self, messages):
        return next(i for i, m in enumerate(messages)
                    if str(m.get("content", "")).startswith("Project guidance snapshot"))

    def test_requests_within_a_run_only_append_even_when_nested_rules_appear(self):
        # 读 src/app.py 会让 src/AGENTS.md 进入规则范围：旧规则已发出，不得就地改写。
        self.rec.script = [_call("read_file", path="README.md"), _call("read_file", path="src/app.py"),
                           _call("read_file", path="README.md"), "done"]
        self.agent.run("看看 greet。")
        calls = self.rec.calls
        self.assertGreaterEqual(len(calls), 4)
        for previous, current in zip(calls, calls[1:]):
            self.assertEqual(current["tools"], previous["tools"])
            self.assertEqual(current["messages"][:len(previous["messages"])], previous["messages"],
                             "下一请求必须以上一请求为前缀（只追加）")
        rule_messages = [m for m in calls[-1]["messages"]
                         if str(m.get("content", "")).startswith("Project guidance snapshot")]
        self.assertEqual(len(rule_messages), 2, "子目录规则以新快照追加，旧快照原样保留")
        self.assertIn("src", rule_messages[-1]["content"])

    def test_rules_sit_before_history_so_next_turn_keeps_the_prefix(self):
        self.rec.script = ["第一轮完成"]
        first = self.agent.run("第一个问题")
        turn1 = self.rec.calls[-1]["messages"]
        self.rec.script = ["第二轮完成"]
        self.agent.run("第二个问题", history=[{"role": "user", "content": "第一个问题"},
                                              {"role": "assistant", "content": first.text}])
        turn2 = self.rec.calls[-1]["messages"]
        index = self._rules_index(turn2)
        history_at = next(i for i, m in enumerate(turn2) if m.get("content") == "第一个问题")
        self.assertLess(index, history_at, "规则必须在历史之前")
        self.assertEqual(turn2[:index + 1], turn1[:index + 1], "system → 记忆 → 规则 跨轮逐字节不变")
        # 只要求本轮提问在末尾；任务消息尾部可能另有运行时投影（如任务契约）。
        self.assertTrue(turn2[-1]["content"].startswith("第二个问题"))


if __name__ == "__main__":
    unittest.main()
