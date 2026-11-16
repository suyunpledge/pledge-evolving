"""Explicit deployment constraints, separate from model identity and cache keys.

Absent declarations preserve the existing wire. Limits are intersections, never
inferred from a reseller name. Token estimates include tools and replay metadata;
they are a local admission estimate, not a tokenizer or billing measurement.
"""
from __future__ import annotations

import copy
import json
import math


_LIMITS = {"contextWindow", "maxOutputTokens"}
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


def default_output(profile, capabilities, *, adapt=True):
    default = (profile.min_output_tokens if adapt else 0) or 2048
    return min(default, capabilities.get("maxOutputTokens", default))


def request_budget(payload, capabilities):
    """Read-only estimate of the final request, including non-content fields."""
    material = {k: payload[k] for k in ("system", "messages", "tools", "tool_choice") if k in payload}
    chars = len(json.dumps(material, ensure_ascii=False, separators=(",", ":")))
    estimated = math.ceil(chars / 2.5) + 64 + 8 * len(payload.get("messages") or [])
    outputs = [payload[k] for k in ("max_tokens", "max_completion_tokens") if k in payload]
    if any(type(v) is not int or v <= 0 for v in outputs):
        raise ValueError("output token limit must be a positive integer")
    reserve = max(outputs, default=capabilities.get("maxOutputTokens", 2048))
    window = capabilities.get("contextWindow")
    budget = max(0, window - reserve) if window is not None else None
    return {"estimated_input_tokens": estimated, "output_reserve": reserve,
            "context_window": window, "input_budget": budget,
            "fits": budget is None or estimated <= budget,
            "estimator": "serialized_chars/2.5+protocol_margin"}


def preview_budget(messages, tools, provider, model):
    caps = provider.capabilities_for_model(model)
    payload = {"messages": messages, "tools": tools,
               "max_tokens": default_output(provider.profile_for_model(model), caps, adapt=provider.adapt)}
    if provider.wire == "anthropic":
        payload["system"] = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        payload["messages"] = [m for m in messages if m.get("role") != "system"]
    return request_budget(payload, caps)


def validate_request(payload, capabilities):
    if capabilities.get("tools") is False and payload.get("tools"):
        raise ValueError("current route does not support tools")
    if capabilities.get("stream") is False and payload.get("stream"):
        raise ValueError("current route does not support streaming")
    if capabilities.get("vision") is False:
        for message in payload.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in
                    {"image", "image_url", "input_image"} for b in content):
                raise ValueError("current route does not support images")
    efforts = capabilities.get("reasoningEfforts")
    effort = payload.get("reasoning_effort", (payload.get("output_config") or {}).get("effort"))
    if efforts is not None and effort is not None and effort not in efforts:
        raise ValueError("requested reasoning effort is not supported by current route")
    budget = request_budget(payload, capabilities)
    if budget["output_reserve"] > capabilities.get("maxOutputTokens", budget["output_reserve"]):
        raise ValueError("requested output exceeds current route maxOutputTokens")
    if not budget["fits"]:
        raise ValueError("estimated request exceeds current route contextWindow (including output reserve)")
    return budget
