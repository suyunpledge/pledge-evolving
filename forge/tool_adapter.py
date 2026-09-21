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
import html
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
        "fix_double_encoded": True,        # MiMo 偶发 JSON 字符串套字符串
        "close_truncated_json": True,
    },
    "claude": {
        # Anthropic 原生通道很稳，但经网关转译后可能出现 str 类型 input
        "fix_double_encoded": True,
    },
    "gpt": {
        # OpenAI 系：arguments 是 JSON 字符串（正常）；并行调用时 id 可能为空
        "fix_double_encoded": True,
    },
    "gemini": {
        # Gemini：args 可能是 dict（正常）或 JSON 字符串；thought_signature 丢失
        "fix_double_encoded": True,
        "fill_name_field": True,
    },
    "grok": {
        "strip_markdown_fence": True,
        "fix_double_encoded": True,
    },
    "ollama": {
        # 本地小模型全家桶：怪癖全开
        "strip_leading_newline": True,
        "strip_markdown_fence": True,
        "parse_tool_call_tags": True,
        "fix_single_quotes": True,
        "fix_trailing_comma": True,
        "fix_double_encoded": True,
        "close_truncated_json": True,
        "parse_json_in_text": True,
        "validate_tool_names": True,
    },
    "7b": {"validate_tool_names": True, "fix_double_encoded": True,
           "close_truncated_json": True, "fix_single_quotes": True},
    "8b": {"validate_tool_names": True, "fix_double_encoded": True,
           "close_truncated_json": True, "fix_single_quotes": True},
    "14b": {"validate_tool_names": True, "fix_double_encoded": True,
            "close_truncated_json": True},
    "small": {"validate_tool_names": True, "fix_double_encoded": True,
              "close_truncated_json": True, "fix_single_quotes": True},
    "phi": {"validate_tool_names": True, "fix_double_encoded": True,
            "fix_single_quotes": True, "close_truncated_json": True},
    "gemma": {"validate_tool_names": True, "fix_double_encoded": True,
              "close_truncated_json": True},
}

# 匹配 ```json ... ``` 或 ``` ... ```
_MARKDOWN_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
# 匹配 <tool_call>{...}</tool_call>

def _find_braced(text: str, start: int) -> int | None:
    """Brace-depth matcher: finds matching '}' for '{' at text[start]."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\" and in_str:
            esc = True
            continue
        if c == '"' and not in_str:
            in_str = True
        elif c == '"' and in_str:
            in_str = False
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    return None


def _extract_braced_objs(text: str) -> list[str]:
    """Extract all top-level {...} objects using brace-depth."""
    objs: list[str] = []
    i = 0
    while i < len(text):
        if text[i] in (" ", "\t", "\n", "\r"):
            i += 1
            continue
        if text[i] != "{":
            i += 1
            continue
        end = _find_braced(text, i)
        if end is None:
            break
        objs.append(text[i : end + 1])
        i = end + 1
    return objs


def _extract_bare_json(text: str) -> str | None:
    """Find longest valid top-level JSON object using brace-depth."""
    best: str | None = None
    for i, c in enumerate(text):
        if c == "{":
            end = _find_braced(text, i)
            if end is not None:
                chunk = text[i : end + 1]
                if chunk.startswith("{") and chunk.endswith("}"):
                    if best is None or len(chunk) > len(best):
                        best = chunk
    return best


# Keep _TOOLS_XML_TAG as regex for XML boundaries; content extracted
# will be parsed with brace-depth fallback inside the loop.
_TOOLS_XML_TAG = re.compile(r"<tools>(.*?)</tools>", re.DOTALL)
_LEADING_WS = re.compile(r"^[\s\n\r\t]+")


def get_quirks(model_name: str) -> dict[str, bool]:
    """按模型名取该模型的修复开关。

    合并所有命中的档案而不是首个命中——"qwen2.5:7b" 应同时拿到
    qwen 档和 7b 档（小模型）的修复开关。后写入的档案不覆盖已有
    True（只增不减，修复开关取并集最安全）。
    """
    name = (model_name or "").lower()
    merged: dict[str, bool] = {}
    for key, quirks in MODEL_QUIRKS.items():
        if key in name:
            for k, v in quirks.items():
                merged[k] = merged.get(k, False) or v
    return merged


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


def _try_double_decode(s: str) -> dict[str, Any] | None:
    """处理双重编码：模型输出 '"{\\"a\\": 1}"'（JSON 字符串套 JSON）。

    先 json.loads 剥外层字符串，再解内层 JSON。最多剥两层。
    """
    cur = s
    for _ in range(2):
        if not (cur.startswith('"') and cur.endswith('"')):
            break
        try:
            inner = json.loads(cur)
        except (json.JSONDecodeError, ValueError):
            break
        if isinstance(inner, dict):
            return inner
        if isinstance(inner, str):
            cur = inner.strip()
            continue
        break
    obj = _try_json(cur) if cur is not s else None
    return obj if isinstance(obj, dict) else None


# ─── 工具名校验（小模型幻觉防线）─────────────────────────

def normalize_tool_name(name: str, known_names: list[str] | None) -> str:
    """校验并修正小模型幻觉的工具名。

    已知失败模式：
      - "functions.web_search" —— 旧 OpenAI 模板残留前缀
      - "Web_Search" / "web-search" —— 大小写/连字符漂移
      - "read_file(path=...)" —— 把参数写进名字里
      - 完全编造的名字 —— 返回原名，由上层拒绝

    能对上 known_names 就修正返回；对不上原样返回（不猜）。
    """
    if not name:
        return name
    n = name.strip()
    # 剥 "functions." / "tools." 前缀
    for prefix in ("functions.", "tools.", "function."):
        if n.lower().startswith(prefix):
            n = n[len(prefix):]
    # 剥 "(...)" 参数尾巴
    if "(" in n:
        n = n[: n.index("(")]
    n = n.strip()
    if not known_names:
        return n
    if n in known_names:
        return n
    # 宽松匹配：小写 + 下划线/连字符归一
    def canon(x: str) -> str:
        return x.lower().replace("-", "_").replace(" ", "_")
    target = canon(n)
    for known in known_names:
        if canon(known) == target:
            return known
    return n  # 对不上就原样返回，由上层裁决


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


    closed = _close_json(s)
    if closed is not None:
        return closed


    closed = _close_json(s)
    if closed is not None:
        return closed


# ─── L4: 截断补全 ────────────────────────────────────────
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

    # L2.5 双重编码：'"{\\"a\\": 1}"' → 先解一层字符串再解 JSON
    if quirks.get("fix_double_encoded", True):
        obj = _try_double_decode(s)
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
    # 用 brace-depth 替代 regex，嵌套结构不再截断
    bare = _extract_bare_json(s)
    if bare is not None:
        obj = _try_json(bare)
        if obj is not None:
            return validate_against_schema(obj, schema)

    # 全部失败 → 保留原文供降级
    return {"_raw": raw_args[:2000]}


# ─── Hermes 风格 <tool_call> 提取 ────────────────────────

def parse_tool_call_tags(content: str) -> list[dict[str, Any]]:
    """从 content 文本中提取 <tool_call>{...}</tool_call> 格式的调用。

    Qwen/Mistral 系（Hermes 模板）在原生工具通道失败时会降级到文本输出。
    """
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_call(raw: str) -> None:
        obj = _try_json(raw)
        if obj is None:
            obj = _try_repair(raw, get_quirks(""))
        if isinstance(obj, dict) and "name" in obj:
            args = obj.get("arguments") or obj.get("parameters") or {}
            # arguments 可能是 JSON 字符串（模型输出 "arguments": "{\"city\":...}"）
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    pass
            key = (obj["name"], json.dumps(args, sort_keys=True))
            if key not in seen:
                seen.add(key)
                calls.append({
                    "id": obj.get("id", ""),
                    "name": obj["name"],
                    "arguments": args if isinstance(args, dict) else {},
                })

    for raw in _extract_braced_objs(content or ""):
        _add_call(raw)
    # <tools> XML tag（qwen2.5-coder 风格）：先取 XML 内容，再 brace-depth 解析
    for m in _TOOLS_XML_TAG.finditer(content or ""):
        raw = html.unescape(m.group(1))  # &quot; → " 等 XML 实体解码
        for chunk in _extract_braced_objs(raw):
            _add_call(chunk)
    # _extract_braced_objs 找不到闭合 '}' 时（截断 JSON），直接 fallback _close_json
    if not calls:
        closed = _close_json(content or "")
        if closed is not None:
            args = closed.get("arguments") or closed.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    pass
            calls.append({
                "id": closed.get("id", ""),
                "name": closed.get("name", ""),
                "arguments": args if isinstance(args, dict) else {},
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
            params["additionalProperties"] = False
        cleaned.append(t)
    return cleaned


# ─── 消息级清洗（发往 API 前的最后防线）──────────────────

def sanitize_messages(messages: list[dict[str, Any]], wire: str) -> list[dict[str, Any]]:
    """按 wire 协议清洗消息序列，避免各家 API 的 400。

    anthropic:
      - tool_result 必须紧跟含 tool_use 的 assistant 消息（成对补齐/剔除孤儿）
      - content 不允许空字符串 → 补占位文本
    openai:
      - role=tool 消息必须有 tool_call_id；孤儿 tool 消息剔除
      - assistant.tool_calls 存在时 content 可为 null（合法），但空串要转 null
    """
    if wire == "anthropic":
        return _sanitize_anthropic(messages)
    return _sanitize_openai(messages)


def _sanitize_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI wire: drop orphan tool msgs + inject placeholders for missing tool_call_id + null-empty-content.

    OpenAI Chat-Completions hard constraint: every tool_call_id from assistant
    must have a corresponding role=tool message or the API returns 400.
    Two-pass scan: collect required ids, then batch-insert from back-to-front
    immediately after each assistant to avoid index drift.
    """
    required_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                cid = call.get("id")
                if cid:
                    required_ids.add(cid)

    out: list[dict[str, Any]] = []
    seen_tool_ids: set[str] = set()
    assistant_indices: list[int] = []

    for msg in messages:
        m = dict(msg)
        role = m.get("role")
        if role == "assistant":
            if m.get("content") == "":
                m["content"] = None
            assistant_indices.append(len(out))
            out.append(m)
        elif role == "tool":
            cid = m.get("tool_call_id")
            if not cid or cid not in required_ids:
                continue  # orphan
            seen_tool_ids.add(cid)
            out.append(m)
        else:
            out.append(m)

    missing = required_ids - seen_tool_ids
    if not missing:
        return out

    # back-to-front insertion so earlier indices stay valid
    for ai in reversed(assistant_indices):
        amsg = out[ai]
        ids_in_msg = [c.get("id") for c in (amsg.get("tool_calls") or []) if c.get("id")]
        lost = [cid for cid in ids_in_msg if cid in missing]
        if not lost:
            continue
        placeholders = [{
            "role": "tool",
            "tool_call_id": cid,
            "content": '{"error": "tool result missing: call was interrupted or dropped"}',
        } for cid in lost]
        # 逐个插入并递增 ai，防止索引偏移把占位插到已有 tool 结果前面
        pos = ai + 1
        for p in placeholders:
            out.insert(pos, p)
            pos += 1

    return out



def _sanitize_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """anthropic: 空 content 补占位；tool_use 后缺 tool_result 时补错误占位。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        m = dict(msg)
        content = m.get("content")
        if isinstance(content, str) and not content.strip():
            m["content"] = "..."
        out.append(m)

    fixed: list[dict[str, Any]] = []
    i = 0
    while i < len(out):
        m = out[i]
        fixed.append(m)
        if m.get("role") != "assistant":
            i += 1
            continue
        use_ids = [b.get("id") for b in (m.get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not use_ids:
            i += 1
            continue
        nxt = out[i + 1] if i + 1 < len(out) else None
        result_ids = set()
        if nxt is not None and nxt.get("role") == "user":
            for b in nxt.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    result_ids.add(b.get("tool_use_id"))
        missing = [uid for uid in use_ids if uid and uid not in result_ids]
        if missing:
            blocks = [{"type": "tool_result", "tool_use_id": uid,
                       "content": "[missing result: call was interrupted]",
                       "is_error": True} for uid in missing]
            if nxt is not None and nxt.get("role") == "user":
                merged = dict(nxt)
                raw_content = merged.get("content")
                # 字符串 content 先转 text 块（list(str) 会拆成单字符！）
                if isinstance(raw_content, str):
                    content = [{"type": "text", "text": raw_content}]
                elif isinstance(raw_content, list):
                    content = list(raw_content)
                else:
                    content = []
                merged["content"] = content + blocks
                out[i + 1] = merged
            else:
                fixed.append({"role": "user", "content": blocks})
        i += 1
    return fixed


def fill_gemini_name_fields(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gemini 要求每条 tool 消息带 name 字段（hermes-agent #16478）。

    OpenAI wire 的 role=tool 消息只有 tool_call_id；发 Gemini 兼容网关前，
    从对应的 assistant.tool_calls 里把函数名抄过来。
    """
    id_to_name: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                cid = call.get("id")
                fn = (call.get("function") or {}).get("name")
                if cid and fn:
                    id_to_name[cid] = fn
    out = []
    for m in messages:
        if m.get("role") == "tool" and "name" not in m:
            m = dict(m)
            m["name"] = id_to_name.get(m.get("tool_call_id") or "", "tool")
        out.append(m)
    return out

