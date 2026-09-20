"""Tool calling adapter — 针对各模型 tool calling 字段的补全与修复。

为什么需要这个模块（都是实战踩过的坑）：

1. **JSON 参数损坏**：Qwen3 会输出 `\n{"name":...}`（带前导换行）；
   DeepSeek 截断时会留半个 JSON；部分模型用单引号（Python dict repr）。
2. **markdown 包裹**：模型把 tool call 包在 ```json ... ``` 里。
3. **Hermes 风格 XML**：Qwen/Mistral 系在 content 里输出 <tool_call> 标签。
4. **参数类型漂移**：数字变字符串、null、嵌套引号转义错误。
5. **vLLM 文本提取**：从原始文本提取的调用格式五花八门。

修复策略（分层，从轻到重）：
  L1  清洗 —— 去空白/markdown 包裹/前导换行
  L2  解析 —— 标准 json.loads
  L3  修复 —— 单引号→双引号、尾逗号、Python literal
  L4  补全 —— 截断 JSON 的括号/引号自动闭合
  L5  校验 —— 对照工具 schema 做类型矫正

每一层失败才进下一层；全部失败时保留原始文本（_raw）供上层降级。
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

# ─── 已知模型怪癖表 ──────────────────────────────────────
# key = 模型名子串（小写匹配），value = 修复开关
MODEL_QUIRKS: dict[str, dict[str, bool]] = {
    "qwen": {
        "strip_leading_newline": True,     # Qwen3: '\n{"name":...}'
        "parse_tool_call_tags": True,      # Hermes 风格 <tool_call> 标签
        "fix_single_quotes": True,         # 偶发 Python dict repr
    },
    "deepseek": {
        "close_truncated_json": True,      # max_tokens 截断时半个 JSON
        "strip_markdown_fence": True,      # ```json 包裹
    },
    "glm": {
        "strip_markdown_fence": True,
        "fix_trailing_comma": True,
    },
    "mistral": {
        "parse_tool_call_tags": True,      # Mistral 也用 <tool_call>
        "fix_single_quotes": True,
    },
    "llama": {
        "parse_json_in_text": True,        # content 里裸 JSON
    },
    "mimo": {
        "strip_markdown_fence": True,
    },
}

# 匹配 ```json ... ``` 或 ``` ... ```
_MARKDOWN_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
# 匹配 <tool_call>{...}</tool_call>
_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# 匹配裸 JSON 对象（在长文本中提取）
_BARE_JSON = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
# 前导空白/换行
_LEADING_WS = re.compile(r"^[\s\n\r\t]+")


def get_quirks(model_name: str) -> dict[str, bool]:
    """按模型名取该模型的修复开关。"""
    name = (model_name or "").lower()
    for key, quirks in MODEL_QUIRKS.items():
        if key in name:
            return quirks
    return {}


# ─── L1: 清洗 ────────────────────────────────────────────

def _clean(raw: str, quirks: dict[str, bool]) -> str:
    """去除前导换行、markdown 包裹、多余空白。"""
    s = raw.strip()
    # 前导换行（Qwen3 怪癖）
    if quirks.get("strip_leading_newline", True):
        s = _LEADING_WS.sub("", s)
    # markdown 代码块包裹
    if quirks.get("strip_markdown_fence", True):
        m = _MARKDOWN_FENCE.match(s)
        if m:
            s = m.group(1).strip()
    return s


# ─── L2: 标准解析 ────────────────────────────────────────

def _try_json(s: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# ─── L3: 修复解析 ────────────────────────────────────────

def _try_repair(s: str, quirks: dict[str, bool]) -> dict[str, Any] | None:
    """单引号→双引号、尾逗号移除、Python literal 解析。"""
    # 尾逗号：{"a": 1,} → {"a": 1}
    if quirks.get("fix_trailing_comma", True):
        s = re.sub(r",\s*([}\]])", r"\1", s)
        first = _try_json(s)
        if first is not None:
            return first
    # 单引号（Python dict repr）→ 尝试 ast.literal_eval
    if quirks.get("fix_single_quotes", True) and ("'" in s):
        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, dict):
                return obj
        except (ValueError, SyntaxError):
            pass
        # 暴力替换引号（对不含嵌套引号的情况有效）
        replaced = s.replace("'", '"')
        second = _try_json(replaced)
        if second is not None:
            return second
    return None


# ─── L4: 截断补全 ────────────────────────────────────────

def _close_json(s: str) -> dict[str, Any] | None:
    """对截断的 JSON 自动闭合括号和引号。

    策略：统计未闭合的 { [ 和未结束的字符串，逐一补上。
    只在截断发生在值中途时有效；补全后仍然是猜测，标记 _truncated。
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    # 构造补全后缀
    suffix = ""
    if in_string:
        suffix += '"'
    for opener in reversed(stack):
        suffix += "}" if opener == "{" else "]"
    if not suffix:
        return None
    repaired = s + suffix
    obj = _try_json(repaired)
    if isinstance(obj, dict):
        obj["_truncated"] = True  # 标记：这是补全的结果
        return obj
    return None


# ─── L5: schema 类型矫正 ──────────────────────────────────

def _coerce_type(value: Any, expected: str) -> Any:
    """按 schema 期望类型矫正参数值。"""
    if expected == "number" and isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    if expected == "integer" and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if expected == "boolean" and isinstance(value, str):
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
    if expected == "string" and isinstance(value, (int, float)):
        return str(value)
    return value


def validate_against_schema(args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """对照工具 schema 矫正参数类型。schema 形如
    {"type": "object", "properties": {"name": {"type": "string"}, ...}}
    """
    if not schema or not isinstance(schema, dict):
        return args
    props = schema.get("properties") or {}
    for key, spec in props.items():
        if key not in args or not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if expected:
            args[key] = _coerce_type(args[key], expected)
    return args


# ─── 主入口：修复 tool call 参数 ──────────────────────────

def repair_arguments(
    raw_args: Any,
    model_name: str = "",
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """修复模型输出的 tool call arguments。

    输入可以是 str（JSON 文本）、dict（已是对象）、None。
    返回修复后的 dict；全部失败时返回 {"_raw": 原文}。
    """
    # 已经是 dict → 直接做 schema 校验
    if isinstance(raw_args, dict):
        return validate_against_schema(dict(raw_args), schema)
    if raw_args is None:
        return {}
    if not isinstance(raw_args, str):
        raw_args = str(raw_args)

    quirks = get_quirks(model_name)

    # L1 清洗
    s = _clean(raw_args, quirks)

    # L2 标准解析
    obj = _try_json(s)
    if obj is not None:
        return validate_against_schema(obj, schema)

    # L3 修复解析
    obj = _try_repair(s, quirks)
    if obj is not None:
        return validate_against_schema(obj, schema)

    # L4 截断补全
    if quirks.get("close_truncated_json", True):
        obj = _close_json(s)
        if obj is not None:
            return validate_against_schema(obj, schema)

    # L5 从长文本中提取裸 JSON（部分模型把调用埋在 content 里）
    m = _BARE_JSON.search(s)
    if m:
        obj = _try_json(m.group(0))
        if obj is not None:
            return validate_against_schema(obj, schema)

    # 全部失败 → 保留原文供降级
    return {"_raw": raw_args[:2000]}


# ─── Hermes 风格 <tool_call> 提取 ────────────────────────

def parse_tool_call_tags(content: str) -> list[dict[str, Any]]:
    """从 content 文本中提取 <tool_call>{...}</tool_call> 格式的调用。

    Qwen/Mistral 系（Hermes 模板）在原生工具通道失败时会降级到文本输出。
    """
    calls = []
    for m in _TOOL_CALL_TAG.finditer(content or ""):
        raw = m.group(1)
        obj = _try_json(raw)
        if obj is None:
            obj = _try_repair(raw, get_quirks(""))
        if isinstance(obj, dict) and "name" in obj:
            calls.append({
                "id": obj.get("id", ""),
                "name": obj["name"],
                "arguments": obj.get("arguments") or obj.get("parameters") or {},
            })
    return calls


# ─── 请求侧补全：发往模型前的字段检查 ────────────────────

def sanitize_request_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """发送前对工具声明做最后检查，避免 API 400。

    已知问题：部分模型严格拒绝 additionalProperties、空 enum、
    缺 description 的参数。
    """
    cleaned = []
    for tool in tools:
        t = dict(tool)
        fn = t.get("function") or t
        params = fn.get("parameters")
        if isinstance(params, dict):
            props = params.get("properties")
            if isinstance(props, dict):
                for pname, pspec in props.items():
                    if isinstance(pspec, dict):
                        # 空 enum 移除（部分 API 拒绝）
                        if pspec.get("enum") == []:
                            pspec.pop("enum", None)
                        # 缺 description 的补一个（anthropic 严格要求）
                        if "description" not in pspec:
                            pspec["description"] = pname
            # additionalProperties 显式 false（部分 API 要求）
            params.setdefault("additionalProperties", False)
        cleaned.append(t)
    return cleaned
