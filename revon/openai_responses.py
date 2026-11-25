"""Native GPT tool calls behind revon's existing chat transport."""
from __future__ import annotations

import json

from .cache_affinity import GPT61_MODELS, official_openai_endpoint

REPLAY = "responses_replay"
TOOL_MODELS = GPT61_MODELS | {"gpt-6-sol"}


def uses_responses(payload, provider):
    messages = payload.get("messages") or []
    return (provider.adapt and payload.get("model") in TOOL_MODELS and official_openai_endpoint(provider)
            and bool(payload.get("tools") or any(
                m.get("role") == "tool" or m.get("tool_calls") or m.get(REPLAY)
                for m in messages if isinstance(m, dict))))


def replay_scope(provider, model):
    return f"{model}:{provider._credential._account_fingerprint}"


def endpoint(provider):
    base = provider.base_url.rstrip("/")
    return base + ("/responses" if base.endswith("/v1") else "/v1/responses")


def _content_parts(content):
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        if not isinstance(part, dict):
            raise ValueError("GPT tool-request content blocks must be objects")
        kind = part.get("type")
        if kind == "text":
            parts.append({**part, "type": "input_text"})
        elif kind == "image_url":
            image = part["image_url"]
            parts.append({"type": "input_image", "image_url": image["url"],
                          "detail": image.get("detail", "auto")})
        else:
            raise ValueError(f"Unsupported GPT tool-request content type {kind!r}")
    return parts


def replay_items(items, scope=None):
    # Carry only opaque reasoning items across turns. User-visible text and
    # tool arguments are replayed from the same data revon actually consumed.
    return [{key: item[key] for key in ("type", "id", "summary", "encrypted_content", "scope") if key in item}
            for item in items if isinstance(item, dict) and item.get("type") == "reasoning"
            and isinstance(item.get("id"), str) and isinstance(item.get("encrypted_content"), str)
            and isinstance(item.get("summary"), list)
            and (scope is None or item.get("scope") == scope)] if isinstance(items, list) else []


def strip_replay(payload):
    messages = payload.get("messages")
    if isinstance(messages, list) and any(isinstance(m, dict) and REPLAY in m for m in messages):
        return {**payload, "messages": [{k: v for k, v in m.items() if k != REPLAY}
                                        if isinstance(m, dict) else m for m in messages]}
    return payload


def to_request(payload, scope):
    items = []
    for msg in payload["messages"]:
        role, content = msg.get("role"), msg.get("content")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": msg["tool_call_id"],
                          "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)})
            continue
        if role == "assistant":
            items.extend({k: v for k, v in item.items() if k != "scope"}
                         for item in replay_items(msg.get(REPLAY), scope))
            if content:
                items.append({"role": "assistant", "content": content})
            for call in msg.get("tool_calls") or []:
                fn = call["function"]
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": fn["name"], "arguments": fn["arguments"]})
            continue
        items.append({"role": role, "content": _content_parts(content)})

    shaped = {k: payload[k] for k in ("model", "stream", "service_tier", "parallel_tool_calls",
                                     "prompt_cache_key", "prompt_cache_options") if k in payload}
    shaped.update(input=items, store=False, include=["reasoning.encrypted_content"])
    for key in ("max_completion_tokens", "max_tokens"):
        if key in payload:
            shaped["max_output_tokens"] = payload[key]
            break
    if payload.get("reasoning_effort"):
        shaped["reasoning"] = {"effort": payload["reasoning_effort"]}
    if payload.get("tools"):
        shaped["tools"] = [{"type": "function", "strict": False, **tool["function"]}
                           for tool in payload["tools"]]
        choice = payload.get("tool_choice")
        if choice is not None:
            shaped["tool_choice"] = ({"type": "function", "name": choice["function"]["name"]}
                                     if isinstance(choice, dict) else choice)
    return shaped


def to_chat(response, scope):
    status = response.get("status")
    if status not in {"completed", "incomplete"}:
        raise ValueError("GPT Responses did not complete")
    output = response.get("output") or []
    text = "".join(part.get("text", "") for item in output if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") == "output_text")
    calls = [{"id": item["call_id"], "type": "function",
              "function": {"name": item["name"], "arguments": item["arguments"]}}
             for item in output if item.get("type") == "function_call"
             and item.get("status") == "completed"] if status == "completed" else []
    message = {"role": "assistant", "content": text or None}
    replay = replay_items(output)
    if replay:
        message[REPLAY] = [{**item, "scope": scope} for item in replay]
    if calls:
        message["tool_calls"] = calls
    finish = "length" if status == "incomplete" else ("tool_calls" if calls else "stop")
    usage = response.get("usage") or {}
    return {"id": response.get("id"), "object": "chat.completion", "model": response.get("model"),
            "service_tier": response.get("service_tier"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                      "completion_tokens": usage.get("output_tokens", 0),
                      "prompt_tokens_details": usage.get("input_tokens_details") or {},
                      "completion_tokens_details": usage.get("output_tokens_details") or {},
                      "total_tokens": usage.get("total_tokens", 0)}}


def stream_chat(response, model, scope):
    """Publish tool calls only from an authoritative completed response."""
    def packet(delta, finish=None, **extra):
        chunk = {"object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}
        return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()

    emitted_text = False
    while line := response.readline(1024 * 1024 + 1):
        if len(line) > 1024 * 1024:
            raise ValueError("GPT Responses frame exceeds 1 MiB")
        if not line.startswith(b"data:"):
            continue
        event = json.loads(line[5:])
        kind = event.get("type")
        if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            emitted_text = True
            yield packet({"content": event["delta"]})
        elif kind in {"response.completed", "response.incomplete"}:
            chat = to_chat(event["response"], scope)
            choice = chat["choices"][0]
            message = choice["message"]
            delta = {key: message[key] for key in (REPLAY, "content") if message.get(key)}
            if emitted_text:
                delta.pop("content", None)
            if message.get("tool_calls"):
                delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(message["tool_calls"])]
            if delta:
                yield packet(delta)
            yield packet({}, choice["finish_reason"], usage=chat["usage"], service_tier=chat["service_tier"])
            yield b"data: [DONE]\n\n"
            return
        elif kind in {"response.failed", "error"}:
            raise ValueError("GPT Responses stream reported a failure")
    raise ValueError("GPT Responses stream ended before completion")
