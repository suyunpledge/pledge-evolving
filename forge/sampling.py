# -*- coding: utf-8 -*-
"""采样参数策略。

两件事实驱动了这个模块：

1. **有些模型只接受默认温度（1.0）**。给它们传 0.7 会被拒或静默忽略，
   所以「要不要发 temperature」必须按模型判断，不能全局统一。
2. **Claude / Anthropic 协议只认 model / max_tokens / messages / system / tools**。
   ``temperature``、``top_p``、``top_k`` 这类采样参数一律不能带——带上去就是
   400。必须走服务端默认值。

而一般 Agent 场景 0.7 比 1.0 更好用（更稳、少发散），所以默认推荐 0.7，
但只要模型属于上面第 1 类，就自动退回 1.0 / 不发。
"""
from __future__ import annotations

# 一键配置里的两档温度。0.7 适合 Agent（更稳），1.0 是多数新模型的唯一合法值。
AGENT_TEMPERATURE = 0.7
DEFAULT_TEMPERATURE = 1.0

# 只接受默认温度的模型（子串匹配，大小写与空格不敏感）。
# 传别的值会被拒或忽略，因此这些一律不发 temperature。
DEFAULT_ONLY_MODELS: tuple[str, ...] = (
    "gpt-6",
    "claude-6",
    "claude-6-luna",
    "claude6",
    "sol",              # Sol
    "kimi-k3",
    "kimi-k2.6",
)

# Anthropic 协议：不发送任何采样参数，必须用服务端默认。
NO_SAMPLING_WIRES: tuple[str, ...] = ("anthropic",)

# 我们绝不会主动生成的采样参数（出现时按协议剥离）。
EXTRA_SAMPLING_PARAMS: tuple[str, ...] = (
    "top_p", "top_k", "presence_penalty", "frequency_penalty",
    "repetition_penalty", "min_p", "typical_p", "seed",
)


def _norm(value: object) -> str:
    """统一成小写、去掉空格与下划线，便于子串比较。

    ``"Claude 6 Luna"`` / ``"claude-6-luna"`` / ``"claude6luna"`` 会归一到同一个形状。
    """
    return "".join(str(value or "").lower().split()).replace("_", "-")


def requires_default_temperature(model: object) -> bool:
    """这个模型是否只接受默认温度（1.0）。"""
    name = _norm(model)
    if not name:
        return False
    flat = name.replace("-", "")          # claude-6 -> claude6
    for pattern in DEFAULT_ONLY_MODELS:
        p = _norm(pattern)
        if p in name or p.replace("-", "") in flat:
            return True
    return False


def allows_temperature(model: object, wire: object = "openai") -> bool:
    """当前 (模型, 协议) 组合能不能带 temperature。"""
    if str(wire or "").strip().lower() in NO_SAMPLING_WIRES:
        return False
    # 模型名带 claude 的（包括走 openai 兼容中转的 Claude）同样不发采样参数。
    if "claude" in _norm(model):
        return False
    if requires_default_temperature(model):
        return False
    return True


def allows_extra_params(model: object, wire: object = "openai") -> bool:
    """能不能带 top_p 这类额外采样参数。"""
    return allows_temperature(model, wire)


def resolve(model: object, wire: object = "openai",
            requested: object = None) -> float | None:
    """算出该不该发 temperature，以及发多少。

    返回 ``None`` 表示**不要把这个字段放进请求体**（用服务端默认）。
    """
    if not allows_temperature(model, wire):
        return None
    if requested is None or requested == "":
        return None
    try:
        value = float(requested)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 2.0:
        return None
    return value


def describe(model: object, wire: object = "openai",
             requested: object = None) -> str:
    """给界面用的一句话说明：这个模型现在会用什么温度、为什么。"""
    name = str(model or "").strip() or "当前模型"
    if str(wire or "").strip().lower() in NO_SAMPLING_WIRES:
        return f"{name}（Claude 协议）不使用 temperature / top_p，走服务端默认"
    if "claude" in _norm(model):
        return f"{name} 系列不支持 temperature / top_p，走服务端默认"
    if requires_default_temperature(model):
        return f"{name} 只支持默认温度 1.0"
    effective = resolve(model, wire, requested)
    if effective is None:
        return f"{name} 未设置温度，走服务端默认"
    return f"{name} 使用 temperature={effective:g}"


def strip_extra_params(payload: dict, model: object,
                       wire: object = "openai") -> dict:
    """按协议把不该出现的采样参数从请求体里摘掉（就地修改并返回）。"""
    if allows_extra_params(model, wire):
        return payload
    for key in EXTRA_SAMPLING_PARAMS:
        payload.pop(key, None)
    if not allows_temperature(model, wire):
        payload.pop("temperature", None)
    return payload
