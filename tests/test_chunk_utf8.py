# -*- coding: utf-8 -*-
"""
测试目标：chunk_text 对多字节内容的边界安全性
样本来源：审查报告 C 的结论「逐字符切分不会切坏 UTF-8 边界」——需实测确认
期望行为：
  - 中文/emoji 分片后，每个分片单独 UTF-8 编码仍合法（无半个码点）
  - 非空白字符不丢失（分片边界会去掉空白，这是有意的）
  - emoji 可能被切在零宽连接符(ZWJ)处导致视觉断裂——只影响观感，不影响编码
"""

import re
import sys
import unicodedata

sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")

from forge.channels import chunk_text


def encodes_cleanly(s: str) -> bool:
    """A str always encodes; the real check is that no lone surrogate survived."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(unicodedata.category(ch) == "Cs" for ch in s)


def strip_ws(s: str) -> str:
    return re.sub(r"\s+", "", s)


# 中文：每字符 3 字节
zh = "中" * 100
parts = chunk_text(zh, 33)
assert all(encodes_cleanly(p) for p in parts), "中文分片必须可编码"
assert "".join(parts) == zh, "无空白时拼回必须等于原文"
assert all(len(p.encode("utf-8")) % 3 == 0 for p in parts), "每个分片应是整字符"

# emoji：含 4 字节码点
emoji = "😀" * 50
parts = chunk_text(emoji, 17)
assert all(encodes_cleanly(p) for p in parts), "emoji 分片必须可编码"
assert "".join(parts) == emoji, "拼回必须等于原文"
assert all(len(p.encode("utf-8")) % 4 == 0 for p in parts), "每个分片应是整码点"

# 混合：中英 emoji + 换行，模拟真实回复
mixed = "hello 世界 😀\n" * 40
parts = chunk_text(mixed, 101)
assert all(encodes_cleanly(p) for p in parts)
# 分片边界会 rstrip()/lstrip("\n")——空白是有意去掉的，只要求非空白字符不丢
assert strip_ws("".join(parts)) == strip_ws(mixed), "非空白字符不得丢失"

# 每个分片的长度受控（分片是为了适配消息上限）
for limit in (10, 33, 100, 1000):
    for text in (zh, emoji, mixed):
        for p in chunk_text(text, limit):
            assert len(p) <= limit, (limit, len(p))
            assert encodes_cleanly(p)

print("chunk_text multibyte safety: OK")
print("  - 中文分片按整字符切分（每片字节数 % 3 == 0）")
print("  - emoji 分片按整码点切分（每片字节数 % 4 == 0）")
print("  - 非空白字符无丢失")
print("  - 结论：审查报告 C 成立——Python str 按码点索引，不会切出半个 UTF-8 序列")

# 已知观感问题（非编码问题）：ZWJ 序列可能被切开
family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
pieces = chunk_text(family * 3, 5)
if len(pieces) > 1:
    print("  注：ZWJ 复合 emoji 可能被切在连接符处（仅观感，编码合法）")
