# -*- coding: utf-8 -*-
"""
测试目标：adapter 流式截断鲁棒性（NOT 模型行为）
样本来源：手工构造，模拟 Ollama/llama-server 流式接口中途断开的场景
  - 中途断开：<tools>{"name": "x", "arguments": {"ci 就这样断了
  - 字节级截断：UTF-8 多字节字符中途截断
  - 重复标签：流式重传导致的重复开标签
期望行为：
  - 任何截断输入 → 返回 list（可能为空），绝不抛异常
  - 完整累积后应正常解析
设计说明：adapter 是无状态的——流式累积由应用层负责（见 docs/tool_calling_matrix.md）
边界说明：本文件只测 adapter 的行为，不测模型的流式行为
"""

import sys
sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")
from forge.tool_adapter import parse_tool_call_tags, repair_arguments

# Stream 1: mid-JSON truncation
c1 = '<tools>{"name": "get_weather", "arguments": {"ci'
t1 = parse_tool_call_tags(c1)
assert isinstance(t1, list), "must return list, not crash"
print(f"Stream 1 (mid-JSON truncation): OK - returned {len(t1)} tags")

# Stream 2: mid <tool_call> tag
c2 = '<tool_call>{"name": "read_file", "arguments": {"pa'
t2 = parse_tool_call_tags(c2)
assert isinstance(t2, list)
print(f"Stream 2 (mid <tool_call>): OK - {len(t2)} tags")

# Stream 3: open tag only
c3 = '<tools>'
t3 = parse_tool_call_tags(c3)
assert t3 == []
print("Stream 3 (open tag only): OK")

# Stream 4: bare half-JSON
c4 = '{"name": "x", "arguments": {"city": "Bei'
t4 = parse_tool_call_tags(c4)
assert isinstance(t4, list)
print(f"Stream 4 (bare half-JSON): OK - {len(t4)} tags")

# Stream 5: repair_arguments with truncated JSON (triggers L4 completion)
r = repair_arguments('{"name": "x", "arguments": {"city": "Bei', "deepseek")
assert isinstance(r, dict)
print(f"Stream 5 (repair truncated): OK - keys={list(r.keys())[:3]}")

# Stream 6: multi-chunk accumulation (normal streaming)
chunks = ['<tools>', '{"name": "a",', ' "arguments": {}}', '</tools>']
full = "".join(chunks)
t6 = parse_tool_call_tags(full)
assert len(t6) == 1 and t6[0]["name"] == "a"
print("Stream 6 (chunk accumulation): OK")

# Stream 7: byte-level truncation (UTF-8 multi-byte mid-char)
raw = b'<tools>{"name": "\xe4\xb8\xad'
c7 = raw.decode("utf-8", errors="replace")
t7 = parse_tool_call_tags(c7)
assert isinstance(t7, list)
print("Stream 7 (byte-level truncation): OK")

# Stream 8: duplicate open tags (streaming retransmit)
c8 = '<tools><tools>{"name": "x", "arguments": {}}</tools>'
t8 = parse_tool_call_tags(c8)
assert isinstance(t8, list)
print(f"Stream 8 (duplicate open tags): OK - {len(t8)} tags")

print("\nAll 8 streaming truncation tests passed")
