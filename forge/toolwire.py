"""Wire-native tool calling.

The loop's first implementation spoke a text protocol (``<tool_call>{...}``)
because it had to work against any backend without an adapter. That is fine for
a prototype and wrong for a finished architecture: every real agent backend has
a native tool channel, and going through text costs a parse step, loses call
ids, and cannot replay a conversation correctly.

This module is the adapter layer:

* tool *declarations* in both wire shapes (OpenAI ``tools[]`` / Anthropic
  ``tools[]``)
* tool *calls* parsed out of a native response
* tool *results* formatted back in the shape the wire expects, carrying the id
  the model handed out

Wire-aware replay matters: after a native call, the next request must contain
the assistant message that *made* the call and the tool message that answered
it, with matching ids. Reconstructing that from prose is guesswork.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .tool_adapter import (
    parse_tool_call_tags,
    repair_arguments,
    sanitize_request_tools,
    normalize_text_call,
    parse_text_protocol_calls,
)

WIRES = ("openai", "anthropic")


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    wire: str = "openai"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_raw(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "args": self.args, "wire": self.wire}


def _spec_schema(spec) -> dict[str, Any]:
    schema = getattr(spec, "schema", None) or {}
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    if "type" not in schema:
        props: dict[str, Any] = {}
        for name, shape in schema.items():
            if isinstance(shape, str):
                # registry shorthand: {"path": "string"} is not a JSON Schema —
                # real API endpoints reject bare type strings as property values
                props[name] = {"type": shape}
            elif isinstance(shape, dict):
                props[name] = shape
            else:
                props[name] = {"type": "string"}
        return {"type": "object", "properties": props}
    return schema


def tool_declarations(specs: Iterable, wire: str = "openai") -> list[dict[str, Any]]:
    """Turn registry specs into a wire-shaped tool declaration list."""
    if wire not in WIRES:
        raise ValueError(f"unknown wire {wire!r}; expected one of {WIRES}")
    out: list[dict[str, Any]] = []
    for spec in specs:
        if wire == "openai":
            out.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": getattr(spec, "description", "") or "",
                    "parameters": _spec_schema(spec),
                },
            })
        else:
            out.append({
                "name": spec.name,
                "description": getattr(spec, "description", "") or "",
                "input_schema": _spec_schema(spec),
            })
    # Cache-alignment invariant: tool declarations must be sorted by name
    # so the same visible set always produces the same token sequence.
    # DSH achieves this with a configurable toolOrder; we sort alphabetically
    # and verify below.
    out.sort(key=lambda d: d.get("function", d).get("name", d.get("name", "")))

    # 发送前最后检查：空 enum / 缺 description / additionalProperties
    return sanitize_request_tools(out)


def parse_tool_calls(message: dict[str, Any], wire: str = "openai",
                     model: str = "") -> list[ToolCall]:
    """Extract native tool calls from a response message of either shape.

    ``model`` feeds the per-model quirk table (MODEL_QUIRKS): without it every
    repair layer falls back to defaults and model-specific fixes never activate
    -- the old ``parse_tool_calls._model`` attribute had no writer anywhere in
    the tree (2026-09-23 audit), i.e. the quirk table was dead wiring.
    """
    calls: list[ToolCall] = []
    if not isinstance(message, dict):
        return calls

    if wire == "openai":
        for row in message.get("tool_calls") or []:
            if not isinstance(row, dict):
                continue
            function = row.get("function") or {}
            raw_args = function.get("arguments")
            # 经 tool_adapter 分层修复：清洗→解析→修复→补全→类型矫正
            args = repair_arguments(raw_args, model_name=model)
            calls.append(ToolCall(id=str(row.get("id") or ""), name=str(function.get("name") or ""),
                                  args=args if isinstance(args, dict) else {}, wire="openai", raw=row))
        if not calls:
            # Hermes 风格降级：模型没走原生通道，把 <tool_call> 埋在 content 里
            content_text = message.get("content") or ""
            if isinstance(content_text, str):
                for tag_call in parse_tool_call_tags(content_text):
                    calls.append(ToolCall(
                        id=str(tag_call.get("id") or ""),
                        name=str(tag_call.get("name") or ""),
                        args=repair_arguments(tag_call.get("arguments"), model_name=model),
                        wire="openai", raw=tag_call))
        return calls

    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            args = repair_arguments(block.get("input"), model_name=model)
            calls.append(ToolCall(id=str(block.get("id") or ""), name=str(block.get("name") or ""),
                                  args=args if isinstance(args, dict) else {}, wire="anthropic",
                                  raw=block))
    return calls


def openai_assistant_message(text: str, calls: list[ToolCall]) -> dict[str, Any]:
    """The assistant turn to replay before answering a native call."""
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [{"id": call.id, "type": "function",
                        "function": {"name": call.name,
                                     "arguments": json.dumps(call.args, ensure_ascii=False)}}
                       for call in calls],
    }


def anthropic_assistant_message(text: str, calls: list[ToolCall]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in calls:
        content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.args})
    return {"role": "assistant", "content": content}


def assistant_message(text: str, calls: list[ToolCall], wire: str) -> dict[str, Any]:
    if wire == "anthropic":
        return anthropic_assistant_message(text, calls)
    return openai_assistant_message(text, calls)


def tool_result_messages(results: list[tuple[ToolCall, str, bool]], wire: str) -> list[dict[str, Any]]:
    """Format tool results in the shape the wire expects, ids preserved.

    ``results`` is ``[(call, content, ok), ...]``.
    """
    out: list[dict[str, Any]] = []
    if wire == "anthropic":
        blocks: list[dict[str, Any]] = []
        for call, content, ok in results:
            blocks.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": _preview(content),
                "is_error": not ok,
            })
        return [{"role": "user", "content": blocks}]

    for call, content, ok in results:
        out.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": (_preview(content) if ok else f"[error] {_preview(content)}"),
        })
    return out


def _preview(text: str, limit: int = 1200) -> str:
    """Head + tail preview for long tool results in provider messages.

    T2（3.5）：超长工具结果不再全文进 messages（provider 计费项），
    改为保留首尾各一半 + 中间截断标记。短文本原样返回，零开销。
    3.6 nit（DS 回执）：1201–limit+标记长度 区间截断无收益（输出
    ≥ 原长），短路原样返回；标记长度用运行时实测，不硬编码。
    """
    if not text or len(text) <= limit:
        return text
    half = limit // 2
    marked = f"{text[:half]}\n…[truncated: {len(text)} chars total]\n{text[-half:]}"
    if len(marked) >= len(text):
        return text
    return marked


def summarize_calls(calls: list[ToolCall]) -> str:
    """A short, human-readable line for logs and event streams."""
    if not calls:
        return ""
    return "; ".join(f"{call.name}({json.dumps(call.args, ensure_ascii=False)[:80]})" for call in calls)


__all__ = [
    "ToolCall",
    "WIRES",
    "anthropic_assistant_message",
    "assistant_message",
    "openai_assistant_message",
    "parse_tool_calls",
    "parse_text_protocol_calls",
    "summarize_calls",
    "tool_declarations",
    "tool_result_messages",
]
