"""Wire translation between the Anthropic Messages API and the OpenAI Chat API.

Why this exists: agent CLIs speak one wire format and refuse to speak another.
Claude Code only talks `/v1/messages` (Anthropic shape), while the cheap model
tiers we actually want to spend money on (MiMo, DeepSeek, Qwen, GLM) expose
`/v1/chat/completions` (OpenAI shape). Instead of fighting the CLI, translate
at the gateway.

Both directions are implemented, including Server-Sent Events, because these
CLIs stream unconditionally: a non-streaming shim will hang them.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


# ---------------------------------------------------------------------------
# request: Anthropic -> OpenAI
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def anthropic_to_openai_request(payload: dict[str, Any], model_map: dict[str, str] | None = None) -> dict[str, Any]:
    model_map = model_map or {}
    messages: list[dict[str, Any]] = []

    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _text_of(system)})

    for message in payload.get("messages") or []:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        texts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                # a tool result is its own OpenAI "tool" message
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or block.get("id") or "unknown",
                    "content": _text_of(block.get("content")) or "(empty)",
                })
            elif btype == "image":
                texts.append("[image omitted by gateway]")

        if tool_calls:
            messages.append({"role": "assistant", "content": "\n".join(texts) or None,
                             "tool_calls": tool_calls})
        elif texts:
            # merge consecutive same-role plain messages
            if messages and messages[-1].get("role") == role and "tool_calls" not in messages[-1]:
                messages[-1]["content"] = f"{messages[-1].get('content', '')}\n{chr(10).join(texts)}"
            else:
                messages.append({"role": role, "content": "\n".join(texts)})

    out: dict[str, Any] = {
        "model": model_map.get(str(payload.get("model", "")), payload.get("model", "")),
        "messages": messages,
    }
    if payload.get("max_tokens"):
        out["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("stream"):
        out["stream"] = True
    if payload.get("tools"):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for tool in payload["tools"]
        ]
    if payload.get("tool_choice") and not isinstance(payload["tool_choice"], str):
        choice = payload["tool_choice"]
        if choice.get("type") == "tool":
            out["tool_choice"] = {"type": "function", "function": {"name": choice.get("name", "")}}
    return out


# ---------------------------------------------------------------------------
# response: OpenAI -> Anthropic (non-streaming)
# ---------------------------------------------------------------------------

def openai_to_anthropic_response(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, list):  # some providers return content parts
        text = _text_of(text)
    if text:
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            parsed = {"_raw": raw_args}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
            "name": function.get("name", ""),
            "input": parsed,
        })

    usage = payload.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": STOP_REASON.get(str(choice.get("finish_reason")), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }


# ---------------------------------------------------------------------------
# response: OpenAI stream -> Anthropic SSE
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class OpenAIStreamTranslator:
    """Feed raw upstream SSE lines; get Anthropic SSE event strings back."""

    def __init__(self, requested_model: str) -> None:
        self.model = requested_model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.started = False
        self.text_open = False
        self.block_index = 0
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.open_tools: dict[int, int] = {}
        self.finish_reason = "end_turn"
        self.output_tokens = 0
        self.input_tokens = 0
        self._sent_stop = False

    # -- helpers ---------------------------------------------------------
    def _start(self) -> list[str]:
        self.started = True
        return [_sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        })]

    def _close_text(self) -> list[str]:
        if not self.text_open:
            return []
        self.text_open = False
        events = [_sse("content_block_stop", {"type": "content_block_stop", "index": self.block_index})]
        self.block_index += 1
        return events

    def _close_tools(self) -> list[str]:
        events: list[str] = []
        for index in sorted(self.open_tools):
            events.append(_sse("content_block_stop",
                               {"type": "content_block_stop", "index": self.open_tools[index]}))
        self.open_tools.clear()
        return events

    def finish(self) -> list[str]:
        if self._sent_stop:
            return []
        events = self._close_text() + self._close_tools()
        events.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.finish_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }))
        events.append(_sse("message_stop", {"type": "message_stop"}))
        self._sent_stop = True
        return events

    # -- main ------------------------------------------------------------
    def feed(self, raw_line: str) -> list[str]:
        line = raw_line.strip()
        if not line or line.startswith(":"):
            return []
        if not line.startswith("data:"):
            return []
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            return self.finish()
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            return []

        usage = payload.get("usage") or {}
        if usage:
            self.input_tokens = int(usage.get("prompt_tokens") or self.input_tokens or 0)
            self.output_tokens = int(usage.get("completion_tokens") or self.output_tokens or 0)

        choice = (payload.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self.finish_reason = STOP_REASON.get(str(choice["finish_reason"]), "end_turn")

        events: list[str] = []
        if not self.started:
            events.extend(self._start())

        text = delta.get("content")
        if text:
            if not self.text_open:
                self.text_open = True
                events.append(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self.block_index,
                    "content_block": {"type": "text", "text": ""},
                }))
            events.append(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.block_index,
                "delta": {"type": "text_delta", "text": text},
            }))

        for call in delta.get("tool_calls") or []:
            position = int(call.get("index", 0))
            events.extend(self._close_text())
            record = self.tool_calls.setdefault(position, {"id": "", "name": "", "args": ""})
            if call.get("id"):
                record["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                record["name"] += function["name"]
            if function.get("arguments"):
                record["args"] += function["arguments"]

            if position not in self.open_tools:
                if not record["id"]:
                    record["id"] = f"toolu_{uuid.uuid4().hex[:12]}"
                index = self.block_index + len(self.open_tools) + (0 if not self.text_open else 0)
                self.open_tools[position] = index
                events.append(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "tool_use", "id": record["id"],
                                      "name": record["name"], "input": {}},
                }))
            if function.get("arguments"):
                events.append(_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.open_tools[position],
                    "delta": {"type": "input_json_delta", "partial_json": function["arguments"]},
                }))

        if choice.get("finish_reason"):
            events.extend(self.finish())
        return events


def translate_stream(lines: Iterable[str], requested_model: str) -> list[str]:
    """Convenience for tests: translate a whole upstream SSE body at once."""
    translator = OpenAIStreamTranslator(requested_model)
    out: list[str] = []
    for line in lines:
        out.extend(translator.feed(line))
    return out


__all__ = [
    "OpenAIStreamTranslator",
    "STOP_REASON",
    "anthropic_to_openai_request",
    "openai_to_anthropic_response",
    "translate_stream",
]
