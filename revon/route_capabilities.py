"""Explicit deployment constraints, separate from model identity and cache keys.

Absent declarations preserve the existing wire. Limits are intersections, never
inferred from a reseller name. Token estimates include tools and replay metadata;
they are a local admission estimate, not a tokenizer or billing measurement.
"""
from __future__ import annotations

import copy
import json
import math
from urllib.parse import urlsplit


_LIMITS = {"contextWindow", "maxInputTokens", "maxOutputTokens", "outputTokenBudget"}
_FLAGS = {"tools", "vision", "stream"}
_FIELDS = _LIMITS | _FLAGS | {"reasoningEfforts", "cacheTailRule"}


def validate_capabilities(value):
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError("route capabilities contain unknown fields or are not an object")
    for key, item in value.items():
        if key in _LIMITS and (type(item) is not int or item <= 0):
            raise ValueError(f"{key} must be a positive integer")
        if key in _FLAGS and type(item) is not bool:
            raise ValueError(f"{key} must be boolean")
        if key == "reasoningEfforts" and (not isinstance(item, list)
                or any(not isinstance(v, str) or not v for v in item)
                or len(set(item)) != len(item)):
            raise ValueError("reasoningEfforts must be a list of unique nonempty strings")
        if key == "cacheTailRule" and item not in ("none", "deepseek-128"):
            raise ValueError("unknown cacheTailRule")
    return copy.deepcopy(value)


def effective_capabilities(route, model):
    out = validate_capabilities(route)
    for key, value in validate_capabilities(model).items():
        if key in out and key in _LIMITS:
            out[key] = min(out[key], value)
        elif key in out and key in _FLAGS:
            out[key] = out[key] and value
        elif key == "reasoningEfforts" and key in out:
            out[key] = [v for v in out[key] if v in value]
        elif key == "cacheTailRule" and key in out and out[key] != value:
            out[key] = "none"
        else:
            out[key] = value
    return out


def native_token_limits(base_url, model):
    """Published context and output ceilings on Xiaomi's own MiMo V2.6 endpoints."""
    if model in ("mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.6-pro-ultraspeed"):
        try:
            address = urlsplit(base_url)
            host = address.hostname or ""
        except ValueError:
            return {}
        if address.scheme == "https" and host.endswith(".xiaomimimo.com"):
            return {"contextWindow": 1048576, "maxOutputTokens": 131072}
    return {}


def default_output(profile, capabilities, *, adapt=True):
    default = capabilities.get("outputTokenBudget") or (profile.min_output_tokens if adapt else 0) or 2048
    return min(default, capabilities.get("maxOutputTokens", default))


def _image_blocks(blocks):
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            yield from (child for child in block["content"] if isinstance(child, dict)
                        and child.get("type") in {"image", "image_url", "input_image"})
        elif block.get("type") in {"image", "image_url", "input_image"}:
            yield block


def request_budget(payload, capabilities, *, automatic_output=False):
    """Read-only estimate of the final request, including non-content fields."""
    material = {k: payload[k] for k in ("system", "messages", "tools", "tool_choice") if k in payload}
    chars = len(json.dumps(material, ensure_ascii=False, separators=(",", ":")))
    image_count = 0
    for message in material.get("messages", []):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, list):
            for block in _image_blocks(content):
                # An image consumes patch/tile tokens independently of its Base64 encoding.
                chars -= len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
                image_count += 1
    estimated = math.ceil(chars / 2.5) + 64 + 8 * len(payload.get("messages") or []) + 4096 * image_count
    outputs = [payload[k] for k in ("max_tokens", "max_completion_tokens") if k in payload]
    if any(type(v) is not int or v <= 0 for v in outputs):
        raise ValueError("output token limit must be a positive integer")
    reserve = max(outputs, default=capabilities.get("maxOutputTokens", 2048))
    window = capabilities.get("contextWindow")
    if automatic_output and window is not None:
        reserve = min(reserve, max(1, window - estimated))
    budget = max(0, window - reserve) if window is not None else None
    if "maxInputTokens" in capabilities:
        ceiling = capabilities["maxInputTokens"]
        budget = min(budget, ceiling) if budget is not None else ceiling
    return {"estimated_input_tokens": estimated, "output_reserve": reserve,
            "context_window": window, "input_budget": budget,
            "fits": budget is None or estimated <= budget,
            "estimator": "serialized_chars/2.5+protocol_margin" + ("+image_allowance" if image_count else "")}


def preview_budget(messages, tools, provider, model):
    caps = provider.capabilities_for_model(model)
    payload = {"messages": messages, "tools": tools,
               "max_tokens": default_output(provider.profile_for_model(model), caps, adapt=provider.adapt)}
    if provider.wire == "anthropic":
        payload["system"] = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        payload["messages"] = [m for m in messages if m.get("role") != "system"]
    return request_budget(payload, caps, automatic_output=True)


def validate_request(payload, capabilities, *, automatic_output=False):
    if capabilities.get("tools") is False and payload.get("tools"):
        raise ValueError("current route does not support tools")
    if capabilities.get("stream") is False and payload.get("stream"):
        raise ValueError("current route does not support streaming")
    if capabilities.get("vision") is False:
        for message in payload.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list) and any(_image_blocks(content)):
                raise ValueError("current route does not support images")
    efforts = capabilities.get("reasoningEfforts")
    effort = payload.get("reasoning_effort", (payload.get("output_config") or {}).get("effort"))
    if efforts is not None and effort is not None and effort not in efforts:
        raise ValueError("requested reasoning effort is not supported by current route")
    budget = request_budget(payload, capabilities, automatic_output=automatic_output)
    if budget["output_reserve"] > capabilities.get("maxOutputTokens", budget["output_reserve"]):
        raise ValueError("requested output exceeds current route maxOutputTokens")
    if budget["estimated_input_tokens"] > capabilities.get("maxInputTokens", budget["estimated_input_tokens"]):
        raise ValueError(f"预计输入 {budget['estimated_input_tokens']} tokens，超过当前模型的输入上限 "
                         f"{capabilities['maxInputTokens']} tokens。请缩短对话或在模型配置中调整。")
    if not budget["fits"]:
        raise ValueError(f"输入约 {budget['estimated_input_tokens']} tokens，预留输出 {budget['output_reserve']} tokens，"
                         f"超出当前上下文长度（contextWindow）{budget['context_window']} tokens。")
    if automatic_output:
        for key in ("max_tokens", "max_completion_tokens"):
            if key in payload:
                payload[key] = min(payload[key], budget["output_reserve"])
    return budget
