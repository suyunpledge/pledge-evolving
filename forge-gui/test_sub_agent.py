"""sub_agent 单元测试：验证移植自 AI Platform 的契约。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sub_agent as sa  # noqa: E402

PROVIDER = {"baseURL": "https://api.example.com/v1", "model": "x-model",
            "wire": "openai", "apiKey": {"$expr": "get('env.T_KEY', '')"}}
ENV = {"T_KEY": "sk-test"}


def agent(aid, role, **kw) -> sa.SubAgent:
    return sa.SubAgent(id=aid, role=role, system_prompt="S", user_message="U", **kw)


class RunSubAgentsTests(unittest.TestCase):
    def test_parallel_order_preserved(self):
        """慢在前快在后，结果顺序仍与输入顺序一致（AI Platform Promise.all 契约）。"""
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            aid = model  # 用 model 标识
            delay = {"m-slow": 0.35, "m-fast": 0.02, "m-mid": 0.15}[aid]
            import time
            time.sleep(delay)
            return f"out-{aid}"
        agents = [agent("1", "慢", model_name="m-slow"),
                  agent("2", "快", model_name="m-fast"),
                  agent("3", "中", model_name="m-mid")]
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            results = sa.run_sub_agents(agents, ENV, default_provider=PROVIDER,
                                        default_model="m-slow")
        self.assertEqual([r.id for r in results], ["1", "2", "3"])
        self.assertEqual([r.output for r in results],
                         ["out-m-slow", "out-m-fast", "out-m-mid"])
        self.assertTrue(all(r.ok for r in results))

    def test_one_failure_does_not_block_others(self):
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            if model == "bad":
                raise RuntimeError("HTTP 500: boom")
            return f"ok-{model}"
        agents = [agent("1", "坏", model_name="bad"),
                  agent("2", "好", model_name="good1"),
                  agent("3", "好2", model_name="good2")]
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            results = sa.run_sub_agents(agents, ENV, default_provider=PROVIDER,
                                        default_model="good1")
        self.assertEqual(results[0].error, "HTTP 500: boom")
        self.assertFalse(results[0].ok)
        self.assertEqual(results[1].output, "ok-good1")
        self.assertEqual(results[2].output, "ok-good2")

    def test_temperature_fixed_0_3_and_isolation(self):
        """分工温度固定 0.3；每路独立调用（消息不串）。"""
        seen = []
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            seen.append((temperature, tuple(m["content"] for m in messages)))
            return "x"
        agents = [agent("1", "a"), agent("2", "b")]
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            sa.run_sub_agents(agents, ENV, default_provider=PROVIDER,
                              default_model="m")
        self.assertEqual(len(seen), 2)
        for temp, contents in seen:
            self.assertEqual(temp, sa.DEFAULT_TEMPERATURE)
            # system + user 各自独立（不共享 messages 列表）
            self.assertTrue(any("S" in c for c in contents))

    def test_context_text_injection_only_when_provided(self):
        seen = []
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            seen.append([m["role"] for m in messages])
            return "x"
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            sa.run_sub_agents(
                [agent("1", "a", context_text="背景知识"),
                 agent("2", "b")],
                ENV, default_provider=PROVIDER, default_model="m")
        # unified: system + user(context) + user(task); no invented assistant ack
        # isolated: system + user = 2
        self.assertEqual(seen[0], ["system", "user", "user"])
        self.assertEqual(len(seen[1]), 2)

    def test_exception_in_worker_bounded(self):
        """单路任何异常都被兜底（结果里是 error，不抛）。"""
        with patch.object(sa, "provider_chat",
                          side_effect=KeyboardInterrupt()):  # BaseException
            results = sa.run_sub_agents(
                [agent("1", "a")], ENV, default_provider=PROVIDER,
                default_model="m")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)


class FormatTests(unittest.TestCase):
    def test_format_truncates_per_agent_and_total(self):
        results = [sa.SubAgentResult(id=str(i), role=f"r{i}", output="x" * 5000)
                   for i in range(3)]
        out = sa.format_sub_agent_results(results, max_chars_per_agent=100,
                                          max_total_chars=250)
        self.assertIn("x" * 100, out)          # 每路截断
        self.assertNotIn("x" * 101, out)
        self.assertIn("被截断以省 token", out)  # 总量截断标记

    def test_format_marks_failures(self):
        results = [sa.SubAgentResult(id="1", role="critic", error="HTTP 401")]
        out = sa.format_sub_agent_results(results)
        self.assertIn("[critic · 失败]", out)
        self.assertIn("HTTP 401", out)

    def test_injection_protocols(self):
        r = [sa.SubAgentResult(id="1", role="researcher", output="事实A")]
        sub = sa.sub_agent_injection(r)
        self.assertIn("[子 Agent 分工结果 — 共 1 个", sub)
        self.assertIn("引用时按 [role] 标记", sub)
        self.assertIn("[researcher]", sub)
        clu = sa.cluster_injection(r)
        self.assertIn("[Agent 集群方案 — 共 1 路", clu)
        self.assertIn("请综合对比后给出最佳实现", clu)


class ClusterTests(unittest.TestCase):
    def test_count_clamped_1_to_4(self):
        with patch.object(sa, "provider_chat", return_value="方案"):
            res = sa.run_cluster("任务", 99, [], ENV,
                                 default_provider=PROVIDER, default_model="m")
        self.assertEqual(len(res), sa.MAX_CLUSTER_LANES)
        self.assertEqual([x.role for x in res],
                         [f"方案 {i}" for i in range(1, 5)])

    def test_lane_model_fallback(self):
        captured = []
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            captured.append((provider, model))
            return "ok"
        lanes = [{"model": "lane-a-model"}]
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            sa.run_cluster("任务", 2, lanes, ENV,
                           default_provider=PROVIDER, default_model="fallback-m")
        self.assertEqual(captured[0][1], "lane-a-model")   # 第一路用 lane 模型
        self.assertEqual(captured[1][1], "fallback-m")      # 第二路回落默认

    def test_cluster_prompt_is_programming(self):
        captured = []
        def fake_chat(provider, env, messages, *, model, temperature,
                      max_tokens, timeout_s):
            captured.append(messages[0]["content"])
            return "ok"
        with patch.object(sa, "provider_chat", side_effect=fake_chat):
            sa.run_cluster("写个爬虫", 1, [], ENV,
                           default_provider=PROVIDER, default_model="m")
        self.assertIn("senior software engineer", captured[0])
        self.assertIn("runnable", captured[0])


class ConfigTests(unittest.TestCase):
    def test_default_config_and_roundtrip(self):
        with patch.object(sa, "config_path",
                          return_value=Path(sa.config_path().parent
                                            / "test-agent-cluster.json")):
            p = sa.config_path()
            if p.exists():
                p.unlink()
            cfg = sa.load_config()
            self.assertFalse(cfg["cluster"]["enabled"])
            self.assertEqual(len(cfg["templates"]), 4)
            cfg["cluster"]["enabled"] = True
            cfg["cluster"]["count"] = 3
            sa.save_config(cfg)
            self.assertTrue(p.exists())
            cfg2 = sa.load_config()
            self.assertTrue(cfg2["cluster"]["enabled"])
            self.assertEqual(cfg2["cluster"]["count"], 3)
            # 默认值补全
            cfg2.pop("memory_mode")
            cfg3 = sa.load_config() if False else cfg2
            p.unlink()

    def test_build_sub_agents_from_presets(self):
        presets = [
            {"id": "p1", "role": "researcher", "system_prompt": "调研",
             "model": "cheap-model"},
            {"id": "p2", "role": "critic", "system_prompt": "评审"},
            {"id": "p3", "role": "off", "enabled": False},
        ]
        enabled = [p for p in presets if p.get("enabled", True)]
        agents = sa.build_sub_agents(enabled, "用户问题", context_text=None)
        self.assertEqual([a.role for a in agents], ["researcher", "critic"])
        self.assertEqual(agents[0].model_name, "cheap-model")
        self.assertIsNone(agents[1].model_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
