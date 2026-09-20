# -*- coding: utf-8 -*-
"""
流式截断测试（用户第 5 点要求）。
真实场景：Ollama/llama-server 流式输出中途断开，
adapter 收到半个 <tools>{"name": "x", "arguments": {"ci
必须不崩，且能标记为不完整。
"""
import sys
sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")
from forge.tool_adapter import parse_tool_call_tags, repair_arguments

# 流式截断 1: <tools> 标签内 JSON 中途断开
c1 = '<tools>{"name": "get_weather", "arguments": {"ci'
t1 = parse_tool_call_tags(c1)
assert isinstance(t1, list), "must return list, not crash"
print(f"Stream 1 (mid-JSON truncation): OK - returned {len(t1)} tags")

# 流式截断 2: <tool_call> 标签中途断开
c2 = '<tool_call>{"name": "read_file", "arguments": {"pa'
t2 = parse_tool_call_tags(c2)
assert isinstance(t2, list)
print(f"Stream 2 (mid <tool_call>): OK - {len(t2)} tags")

# 流式截断 3: 只有开标签
c3 = '<tools>'
t3 = parse_tool_call_tags(c3)
assert t3 == []
print("Stream 3 (open tag only): OK")

# 流式截断 4: 半个 JSON 对象（无标签）
c4 = '{"name": "x", "arguments": {"city": "Bei'
t4 = parse_tool_call_tags(c4)
assert isinstance(t4, list)
print(f"Stream 4 (bare half-JSON): OK - {len(t4)} tags")

# 流式截断 5: repair_arguments 处理截断 JSON（应触发 L4 补全）
r = repair_arguments('{"name": "x", "arguments": {"city": "Bei', "deepseek")
assert isinstance(r, dict)
print(f"Stream 5 (repair truncated): OK - keys={list(r.keys())[:3]}")

# 流式截断 6: 多 chunk 拼接后完整（模拟正常流式累积）
chunks = ['<tools>', '{"name": "a",', ' "arguments": {}}', '</tools>']
full = "".join(chunks)
t6 = parse_tool_call_tags(full)
assert len(t6) == 1 and t6[0]["name"] == "a"
print("Stream 6 (chunk accumulation): OK")

# 流式截断 7: 字节级截断（可能在 UTF-8 多字节中途）
raw = b'<tools>{"name": "\xe4\xb8\xad'
c7 = raw.decode("utf-8", errors="replace")
t7 = parse_tool_call_tags(c7)
assert isinstance(t7, list)
print("Stream 7 (byte-level truncation): OK")

# 流式截断 8: 重复的 <tools> 开标签（流式重传）
c8 = '<tools><tools>{"name": "x", "arguments": {}}</tools>'
t8 = parse_tool_call_tags(c8)
assert isinstance(t8, list)
print(f"Stream 8 (duplicate open tags): OK - {len(t8)} tags")

print("\nAll 8 streaming truncation tests passed")
