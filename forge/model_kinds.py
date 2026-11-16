"""Stable model-kind identities, independent of reseller/provider row names.

These are capability fingerprints, not proof of a remote model's weights.
Only observed or configured traits are specialised. Unknown versions inherit
family/manufacturer defaults rather than pretending to be a tested sibling.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ModelKind:
    id: str
    manufacturer_id: str
    family_id: str
    canonical_model: str
    aliases: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    overrides: tuple[tuple[str, Any], ...] = ()
    traits: tuple[str, ...] = ()
    evidence: str = "configured"


def _kind(id, manufacturer, family, canonical, *, aliases=(), prefixes=(),
          overrides=(), traits=(), evidence="configured"):
    return ModelKind(id, manufacturer, family, canonical, tuple(aliases), tuple(prefixes),
                     tuple(overrides), tuple(traits), evidence)


_KINDS = (
    _kind("deepseek.v4.flash", "deepseek", "deepseek.v4", "deepseek-flash",
          aliases=("deepseek-v4-flash",), traits=("thinking_type_and_effort", "native_tool_replay"), evidence="live"),
    _kind("deepseek.v4.pro", "deepseek", "deepseek.v4", "deepseek-v4-pro",
          traits=("thinking_type_and_effort", "native_tool_replay"), evidence="live"),
    _kind("deepseek.v4.1.flash", "deepseek", "deepseek.v4.1", "deepseek-v4.1-flash",
          traits=("prompt_cache_details",), evidence="registry_trace"),
    _kind("mimo.v2.6.flash", "mimo", "mimo.v2.6", "mimo-v2.6-flash",
          traits=("thinking_type", "nullable_cache_details", "native_tool_replay",
                  "auto_tool_choice_only", "fixed_sampling_when_thinking"), evidence="live"),
    _kind("mimo.v2.6.pro", "mimo", "mimo.v2.6", "mimo-v2.6-pro",
          traits=("thinking_type", "nullable_cache_details", "native_tool_replay",
                  "auto_tool_choice_only", "fixed_sampling_when_thinking"), evidence="live"),
    _kind("mimo.v2.6.pro-ultraspeed", "mimo", "mimo.v2.6", "mimo-v2.6-pro-ultraspeed",
          traits=("token_plan_model_rejected",), evidence="live_rejection"),
    _kind("minimax.m3", "minimax", "minimax.m3", "minimax-m3",
          overrides=(("steps_hint", 24), ("memory_char_budget", 1000), ("memory_min_score", .30)),
          traits=("adaptive_thinking", "effort_levels", "leading_think_block", "native_tool_replay"), evidence="live"),
    _kind("minimax.m2.7", "minimax", "minimax.m2.7", "minimax-m2.7",
          overrides=(("steps_hint", 0), ("memory_char_budget", 1500), ("memory_min_score", 0.0)),
          traits=("adaptive_thinking",), evidence="registry"),
    _kind("minimax.m2.7.highspeed", "minimax", "minimax.m2.7", "minimax-m2.7-highspeed",
          overrides=(("steps_hint", 0), ("memory_char_budget", 1500), ("memory_min_score", 0.0)),
          traits=("adaptive_thinking", "effort_levels", "native_tool_replay"), evidence="live"),
    _kind("doubao.seed-evolving", "volcengine", "doubao.seed", "doubao-seed-evolving",
          traits=("thinking_type", "opaque_assistant_replay", "native_tool_replay"), evidence="live"),
    _kind("doubao.seed2.1.turbo", "volcengine", "doubao.seed2.1", "doubao-seed-2.1-turbo",
          traits=("thinking_type", "native_tool_replay"), evidence="live"),
    _kind("hunyuan.hy3", "hunyuan", "hunyuan.hy3", "hy3", prefixes=("hy3-",),
          overrides=(("reasoning_efforts", ("low", "high")), ("can_disable_thinking", False)),
          traits=("always_reasoning",), evidence="registry_trace"),
    _kind("hunyuan.hy4", "hunyuan", "hunyuan.hy4", "hy4", prefixes=("hy4-", "hy4preview-"),
          overrides=(("reasoning_efforts", ("high",)), ("can_disable_thinking", False)),
          traits=("always_reasoning",), evidence="registry"),
    _kind("hunyuan.hy4.free", "hunyuan", "hunyuan.hy4", "hy4free", aliases=("hy4-free",),
          overrides=(("reasoning_efforts", ("high",)), ("can_disable_thinking", False)),
          traits=("always_reasoning", "workbuddy_stream_only", "workbuddy_tool_choice_string",
                  "native_tool_replay", "workbuddy_zero_credit_observed"), evidence="live_workbuddy"),
    _kind("hunyuan.hy4.standard", "hunyuan", "hunyuan.hy4", "hy4std", aliases=("hy4-std",),
          overrides=(("reasoning_efforts", ("high",)), ("can_disable_thinking", False)),
          traits=("always_reasoning",), evidence="registry"),
    _kind("glm.5.3", "zhipu", "glm.5.3", "glm-5.3",
          traits=("thinking_type",), evidence="archived_probe"),
    _kind("glm.5.3.flash", "zhipu", "glm.5.3", "glm-5.3-flash",
          traits=("thinking_type",)),
    _kind("qwen3.8.max", "bailian", "qwen3.8", "qwen3.8-max",
          traits=("enable_thinking_documented",), evidence="subscription_unavailable"),
    _kind("qwen3.8.flash", "bailian", "qwen3.8", "qwen3.8-flash",
          traits=("enable_thinking_documented",), evidence="subscription_unavailable"),
    _kind("kimi.k3", "kimi", "kimi.k3", "kimi-k3", aliases=("k3", "k3-256k", "kimi-k3-1", "kimi-for-coding"),
          prefixes=("kimi-k3-", "k3-", "kimi-for-coding-"),
          overrides=(("can_disable_thinking", False),), traits=("always_reasoning",), evidence="registry"),
    _kind("kimi.k3.1.pro", "kimi", "kimi.k3.1", "k3.1-pro", aliases=("kimi-k3.1-pro",),
          prefixes=("k3.1-pro-", "kimi-k3.1-pro-"),
          overrides=(("can_disable_thinking", False),), traits=("always_reasoning",), evidence="registry"),
    _kind("kimi.k2.8", "kimi", "kimi.k2.8", "kimi-k2.8", aliases=("k2.8",),
          prefixes=("kimi-k2.8-",), evidence="registry"),
    _kind("kimi.k2.6", "kimi", "kimi.k2.6", "kimi-k2.6", evidence="configured"),
    _kind("openai.gpt6.sol", "openai", "openai.gpt6", "gpt-6-sol"),
    _kind("openai.gpt6.1.sol", "openai", "openai.gpt6.1", "gpt-6.1-sol"),
    _kind("anthropic.claude.sonnet5", "anthropic", "anthropic.claude5", "claude-sonnet-5"),
    _kind("anthropic.claude.opus5.5", "anthropic", "anthropic.claude5", "claude-opus-5-5"),
    _kind("google.gemini3.8.flash", "gemini", "google.gemini3.8", "gemini-3.8-flash",
          aliases=("gemini-3.8-flash-tiered",), evidence="local_client"),
    _kind("stepfun.step5", "stepfun", "stepfun.step5", "step-5-preview",
          traits=("always_reasoning",), evidence="registry_trace"),
)

MODEL_KINDS = MappingProxyType({kind.id: kind for kind in _KINDS})

# Anchored manufacturer fallback: a substring such as foo1 is not OpenAI o1.
_MANUFACTURERS = (
    (r"deepseek(?:[-.]|$)", "deepseek"), (r"mimo(?:[-.]|$)", "mimo"),
    (r"(?:minimax|abab)(?:[-.]|$)", "minimax"), (r"(?:doubao|seed)(?:[-.]|$)", "volcengine"),
    (r"(?:hunyuan|hy[34])(?:[-.]|$)", "hunyuan"), (r"(?:glm|cogview|cogvideo|codegeex|autoglm)(?:[-.]|$)", "zhipu"),
    (r"(?:qwen|wan)[0-9-]", "bailian"), (r"(?:kimi|moonshot)(?:[-.]|$)", "kimi"),
    (r"(?:gpt-|o[134](?:-|$)|chatgpt)", "openai"), (r"claude(?:[-.]|$)", "anthropic"),
    (r"(?:gemini|gemma)(?:[-.]|$)", "gemini"), (r"(?:step-|stepfun)", "stepfun"),
)


def normalise_model_name(model: str) -> str:
    name = str(model or "").strip().lower()
    if "://" in name:
        return ""
    # Provider namespaces are transport labels; preserve only the model part
    # for matching, never rewrite the actual model sent over the wire.
    return re.sub(r"\s+", "-", name.rsplit("/", 1)[-1]).replace("_", "-")


_ALIASES = {normalise_model_name(alias): kind for kind in _KINDS
            for alias in (kind.id, kind.canonical_model, *kind.aliases)}


def resolve_model_kind(model: str = "", model_kind_id: str = "") -> tuple[ModelKind | None, str]:
    if model_kind_id:
        kind = MODEL_KINDS.get(model_kind_id)
        if kind is None:
            raise ValueError(f"Unknown model kind ID: {model_kind_id!r}")
        return kind, "explicit_kind"
    name = normalise_model_name(model)
    if name in _ALIASES:
        return _ALIASES[name], "model_id"
    matches = [(len(prefix), kind) for kind in _KINDS for prefix in kind.prefixes
               if name.startswith(prefix)]
    if matches:
        return max(matches, key=lambda pair: pair[0])[1], "family_prefix"
    return None, "unknown"


def manufacturer_for_model(model: str) -> str:
    kind, _ = resolve_model_kind(model)
    if kind:
        return kind.manufacturer_id
    name = normalise_model_name(model)
    return next((manufacturer for pattern, manufacturer in _MANUFACTURERS if re.match(pattern, name)), "")


__all__ = ["MODEL_KINDS", "ModelKind", "manufacturer_for_model", "normalise_model_name", "resolve_model_kind"]
