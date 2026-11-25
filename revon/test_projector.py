"""投影层性质测试：装配顺序、纯度、规则合并三结局、指纹稳定性。

这些不变式此前只能靠 test_prefix_cache 的黑盒录制比对间接守护；抽出纯
函数后在此直接锁定（吸收自 Loom project() 的做法）。
"""

from __future__ import annotations

import unittest

from .projector import append_turn, head, head_digest, merge_rules


def _msg(role, content):
    return {"role": role, "content": content}


class ProjectorTests(unittest.TestCase):
    def test_head_order_and_stability(self):
        memory = [_msg("user", "[长期记忆上下文]\nm1")]
        a = head("SYS", memory)
        b = head("SYS", memory)
        self.assertEqual(a[0]["role"], "system")
        self.assertEqual(a[0]["content"], "SYS")
        self.assertEqual(a[1], memory[0])
        self.assertEqual(a, b)
        with self.assertRaises(ValueError):
            head("", memory)

    def test_append_turn_ordering_and_purity(self):
        messages = head("SYS", [])
        history = [_msg("user", "q1"), _msg("assistant", "a1")]
        tail = [_msg("user", "[运行环境] ENVELOPE")]
        out = append_turn(messages, history, tail, "task")
        roles = [m["role"] for m in out]
        self.assertEqual(roles, ["system", "user", "assistant", "user", "user"])
        self.assertEqual(out[-1]["content"], "task")
        self.assertEqual(out[1]["content"], "q1")
        self.assertEqual(out[2]["content"], "a1")
        self.assertEqual(out[3]["content"], "[运行环境] ENVELOPE")
        # 纯度：输入列表不被修改
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(history), 2)

    def test_merge_rules_unchanged(self):
        messages = head("SYS", []) + [_msg("user", "R1")]
        out, outcome = merge_rules(messages, old_context="R1",
                                   snapshot_context="R1", previous_sources=(),
                                   prefix_sent=3)
        self.assertEqual(outcome, "unchanged")
        self.assertEqual(out, messages)

    def test_merge_rules_extension_appends_at_prefix_edge(self):
        messages = [_msg("system", "SYS"), _msg("user", "R1"),
                    _msg("user", "q1"), _msg("assistant", "a1"),
                    _msg("user", "task")]
        out, outcome = merge_rules(messages, old_context="R1",
                                   snapshot_context="R1+src",
                                   previous_sources=("AGENTS.md",),
                                   snapshot_sources=("AGENTS.md", "src/AGENTS.md"),
                                   prefix_sent=3)
        self.assertEqual(outcome, "appended")
        # 旧快照原样保留，新快照接在已发送前缀（索引 3）之后
        self.assertEqual(out[1]["content"], "R1")
        self.assertEqual(out[3]["content"], "R1+src")
        # 已发送前缀（前 3 条）逐字节不变
        self.assertEqual(out[:3], messages[:3])

    def test_merge_rules_modification_replaces(self):
        messages = [_msg("system", "SYS"), _msg("user", "R1"),
                    _msg("user", "q1")]
        # AGENTS.md 不在新来源清单（被改写）→ 非纯新增 → 移除重插
        out, outcome = merge_rules(messages, old_context="R1",
                                   snapshot_context="R2",
                                   previous_sources=("AGENTS.md",),
                                   snapshot_sources=("AGENTS.md@v2",),
                                   prefix_sent=3)
        self.assertEqual(outcome, "replaced")
        self.assertNotIn("R1", [m.get("content") for m in out])
        self.assertEqual(out[1]["content"], "R2")  # 插回原位置

    def test_merge_rules_removed_when_snapshot_empty(self):
        messages = [_msg("system", "SYS"), _msg("user", "R1"), _msg("user", "q1")]
        out, outcome = merge_rules(messages, old_context="R1",
                                   snapshot_context="", previous_sources=(),
                                   prefix_sent=3)
        self.assertEqual(outcome, "removed")
        self.assertEqual(len(out), 2)

    def test_head_digest_stable_and_sensitive(self):
        self.assertEqual(head_digest("SYS", ["a", "b"]),
                         head_digest("SYS", ["a", "b"]))
        self.assertNotEqual(head_digest("SYS", ["a", "b"]),
                            head_digest("SYS", ["a", "c"]))


if __name__ == "__main__":
    unittest.main()
