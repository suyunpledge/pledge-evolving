# -*- coding: utf-8 -*-
"""
Malformed <tools> XML edge cases:
- Truncated JSON inside <tools>
- Unclosed <tools> tag
- Partial/malformed JSON
These should NOT crash the adapter.
"""
import sys
sys.path.insert(0, r"C:\Users\匡溯昀\pledge-evolving")
from forge.tool_adapter import parse_tool_call_tags, repair_arguments

# Malformed 1: truncated JSON inside <tools>
c1 = '<tools>{"name": "get_weather", "arguments": {"city": "Bei'
t1 = parse_tool_call_tags(c1)
assert len(t1) == 0 or (len(t1) == 1 and t1[0].get("arguments", {}).get("_raw")),     f"truncated should fail gracefully: {t1}"
print("Malformed 1 (truncated JSON): OK - no crash")

# Malformed 2: unclosed <tools> tag
c2 = '<tools>{"name": "x", "arguments": {}}'  # missing </tools>
t2 = parse_tool_call_tags(c2)
assert len(t2) == 0, f"unclosed should not match: {t2}"
print("Malformed 2 (unclosed tag): OK - no crash")

# Malformed 3: empty arguments object
c3 = '<tools>{"name": "x", "arguments": {}}</tools>'
t3 = parse_tool_call_tags(c3)
assert len(t3) == 1 and t3[0]["arguments"] == {}
print("Malformed 3 (empty arguments): OK")

# Malformed 4: completely invalid JSON in <tools>
c4 = '<tools>not json at all</tools>'
t4 = parse_tool_call_tags(c4)
assert len(t4) == 0
print("Malformed 4 (invalid JSON): OK - no crash")

# Malformed 5: nested braces without proper JSON
c5 = '<tools>{"name": "x", "arguments": {"nested": {"deep": {}}}}</tools>'
t5 = parse_tool_call_tags(c5)
assert len(t5) == 1
print("Malformed 5 (nested braces): OK")

# Malformed 6: repair_arguments with completely garbled input
r1 = repair_arguments("\\x00\\x01\\x02", "glm4")
assert "_raw" in r1, f"garbled should fallback to _raw: {r1}"
print("Malformed 6 (garbled bytes): OK - falls back to _raw")

# Malformed 7: repair_arguments with partial JSON
r2 = repair_arguments('{"name": "x", "incomplete', "qwen2.5-coder")
# Should either parse (if truncation repair works) or return _raw
assert isinstance(r2, dict), "should return dict"
print("Malformed 7 (partial JSON): OK")

print("\nAll 7 malformed input tests passed - adapter is robust")
