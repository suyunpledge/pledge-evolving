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

    def test_mode_switch_keeps_head_bytes_and_rides_the_tail(self):
        self.rec.script = ["ok"]
        self.agent.run("第一问")
        head1 = self.rec.calls[-1]["messages"][0]["content"]
        # 切换权限模式 + 注入开发指引（Coding Mode 每任务边界都改 extra_system）
        from dataclasses import replace
        self.agent.policy = replace(self.agent.policy, mode=Mode.PLAN)
        self.agent.extra_system = "Development guidance."
        self.rec.script = ["ok"]
        self.agent.run("第二问")
        turn2 = self.rec.calls[-1]["messages"]
        self.assertEqual(turn2[0]["content"], head1, "头部跨模式切换必须逐字节不变")
        env = [m for m in turn2 if str(m.get("content", "")).startswith("[运行环境]")]
        self.assertEqual(len(env), 1, "运行环境信封恰好一条")
        self.assertIn("plan", env[0]["content"].lower())
        self.assertIn("Development guidance", env[0]["content"])
        self.assertLess(turn2.index(env[0]), len(turn2) - 1, "信封在本轮任务之前")
        self.assertTrue(turn2[-1]["content"].startswith("第二问"))

    def test_memory_growth_appends_delta_without_rewriting_the_head(self):
        self.rec.script = ["ok"]
        self.agent.run("第一问")
        turn1 = self.rec.calls[-1]["messages"]
        mem1 = next((m for m in turn1
                     if str(m.get("content", "")).startswith("[长期记忆上下文]")), None)
        self.assertIsNotNone(mem1, "夹具记忆应注入冻结切片")
        # 记忆增长（用户会话中途 remember）
        self.agent.memory.remember("新的踩坑教训", kind="pitfall", source="human")
        self.rec.script = ["ok"]
        self.agent.run("第二问")
        turn2 = self.rec.calls[-1]["messages"]
        mem2 = next((m for m in turn2
                     if str(m.get("content", "")).startswith("[长期记忆上下文]")), None)
        self.assertIsNotNone(mem2)
        self.assertEqual(mem2["content"], mem1["content"], "冻结切片不得改写")
        delta = [m for m in turn2 if str(m.get("content", "")).startswith("[长期记忆更新]")]
        self.assertEqual(len(delta), 1, "增量恰好播报一次")
        self.assertIn("新的踩坑教训", delta[0]["content"])
        # 无新增的后续轮次不再播报
        self.rec.script = ["ok"]
        self.agent.run("第三问")
        turn3 = self.rec.calls[-1]["messages"]
        self.assertEqual(
            [m for m in turn3 if str(m.get("content", "")).startswith("[长期记忆更新]")],
            [], "无增长时不得重复播报")

    def test_cache_defer_guard_formula_boundaries(self):
        # Tianshu protection 公式四边界 + 无数据不延迟
        b = {"fits": True, "input_budget": 100000, "estimated_input_tokens": 30000}
        self.agent._recent_cache_rate = 0.8
        guard = self.agent._cache_defer_guard(b)  # 0.8×0.7=0.56 ≥0.45 → 延迟
        self.assertIsNotNone(guard)
        self.assertAlmostEqual(guard["protection"], 0.56, places=2)
        # 压力升高：0.8×0.3=0.24 → 不延迟
        self.assertIsNone(self.agent._cache_defer_guard(
            {"fits": True, "input_budget": 100000, "estimated_input_tokens": 70000}))
        # 命中率低：0.2×0.9=0.18 → 不延迟
        self.agent._recent_cache_rate = 0.2
        self.assertIsNone(self.agent._cache_defer_guard(b))
        # 无命中率数据 → 不延迟（照旧压缩）
        self.agent._recent_cache_rate = None
        self.assertIsNone(self.agent._cache_defer_guard(b))
        # 无预算数字 → 不延迟
        self.agent._recent_cache_rate = 0.9
        self.assertIsNone(self.agent._cache_defer_guard({"fits": True}))
        self.assertIsNone(self.agent._cache_defer_guard({}))

    def test_rules_sit_before_history_so_next_turn_keeps_the_prefix(self):
        self.rec.script = ["第一轮完成"]
        first = self.agent.run("第一个问题")
        turn1 = self.rec.calls[-1]["messages"]
        self.rec.script = ["第二轮完成"]
        self.agent.run("第二个问题", history=[{"role": "user", "content": "第一个问题"},
                                              {"role": "assistant", "content": first.text}])
        turn2 = self.rec.calls[-1]["messages"]
        index = self._rules_index(turn2)
        history_at = next(i for i, m in enumerate(turn2) if str(m.get("content", "")).startswith("第一个问题"))
        self.assertLess(index, history_at, "规则必须在历史之前")
        self.assertEqual(turn2[:index + 1], turn1[:index + 1], "system → 记忆 → 规则 跨轮逐字节不变")
        # 只要求本轮提问在末尾；任务消息尾部可能另有运行时投影（如任务契约）。
        self.assertTrue(turn2[-1]["content"].startswith("第二个问题"))


if __name__ == "__main__":
    unittest.main()
