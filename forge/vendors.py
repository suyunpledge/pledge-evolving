"""Vendor adaptation layer — one request shape per billing model.

Why this module exists
----------------------
Seven vendors, seven billing shapes. The same ``messages`` list sent to two
providers produces two different invoices, and a few of the differences are
not cosmetic — they change what you are allowed to send:

* **Cache control is a request field on some vendors and a no-op on others.**
  Anthropic needs ``cache_control`` on the block you want cached; DeepSeek,
  OpenAI, Gemini and GLM cache automatically and ignore the marker. Sending
  the marker to a vendor that does not know the field is a 400 risk, so the
  injection is gated per profile instead of being global.

* **Reasoning tokens are billed as output.** Gemini says so outright; the
  thinking modes of DeepSeek and GLM behave the same way. A budget decision
  therefore has to know whether "more thinking" costs input rates or output
  rates — a 4–5x difference.

* **Peak/valley pricing exists on exactly one vendor.** DeepSeek halves every
  rate off-peak. Nothing in the request changes; only *when* it is sent. The
  scheduler has to be able to ask "is this an expensive window right now".

* **Long context is a cliff, not a slope.** OpenAI and Gemini double the rate
  once the prompt crosses a threshold. Crossing it to preserve 100 tokens of
  history is a bad trade, and the decision needs to be visible to the caller.

This module is deliberately data-first: ``PROFILES`` is a table, detection is
a function over that table, and every other module asks the table rather than
hard-coding a vendor name.

Usage::

    from .vendors import profile_for, shape_anthropic_cache, normalize_usage

    profile = profile_for(base_url="https://api.deepseek.com", model="deepseek-flash")
    profile.peak_valley        # True
    profile.cache_read_multiplier  # 0.02
"""

from __future__ import annotations

import math
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VendorProfile:
    """Everything the framework needs to know about one vendor's billing shape."""

    id: str
    label: str
    wire: str                      # preferred wire format: "openai" | "anthropic"
    # -- cache ---------------------------------------------------------------
    cache_mode: str                # implicit | explicit_anthropic | explicit_openai | none
    cache_read_multiplier: float   # hit price / base input price
    cache_write_multiplier: float  # write price / base input price (1.0 = not charged)
    min_cache_tokens: int          # prefix shorter than this is never cached
    cache_ttl: str                 # human label: "5m" | "30m" | "1h" | "disk" | "managed"
    # -- modifiers -----------------------------------------------------------
    batch_discount: float          # multiplier when using the batch API (0.5 = half)
    supports_flex: bool            # cheaper-but-slower service tier available
    peak_valley: bool              # time-of-day pricing
    # -- long context --------------------------------------------------------
    long_context_threshold: int    # 0 = no cliff
    long_context_multiplier: float  # applied once the prompt crosses the threshold
    # -- reasoning -----------------------------------------------------------
    reasoning_billed_as_output: bool
    # -- request shaping -----------------------------------------------------
    inject_cache_control: bool     # default policy for putting the marker on the wire
    # -- telemetry -----------------------------------------------------------
    usage_aliases: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def explicit_cache(self) -> bool:
        return self.cache_mode.startswith("explicit")

    def to_raw(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "wire": self.wire,
            "cache_mode": self.cache_mode,
            "cache_read_multiplier": self.cache_read_multiplier,
            "cache_write_multiplier": self.cache_write_multiplier,
            "min_cache_tokens": self.min_cache_tokens,
            "cache_ttl": self.cache_ttl,
            "batch_discount": self.batch_discount,
            "supports_flex": self.supports_flex,
            "peak_valley": self.peak_valley,
            "long_context_threshold": self.long_context_threshold,
            "long_context_multiplier": self.long_context_multiplier,
            "reasoning_billed_as_output": self.reasoning_billed_as_output,
            "inject_cache_control": self.inject_cache_control,
        }


# Canonical usage keys every vendor is normalised onto.
CANONICAL_USAGE_KEYS = (
    "input_tokens",         # total prompt-side tokens (cached + uncached)
    "cached_tokens",        # subset of input that was a cache read
    "cache_write_tokens",   # tokens written to cache on this request
    "output_tokens",        # generated tokens
    "reasoning_tokens",     # subset of output spent on thinking
)

# Shared alias table: raw field name -> canonical key. Vendor rows extend this.
_BASE_USAGE_ALIASES: dict[str, str] = {
    # OpenAI-shaped
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "cache_write_tokens": "cache_write_tokens",
    # Anthropic-shaped
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    # reasoning
    "reasoning_tokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "thoughts_token_count": "reasoning_tokens",
}


# --------------------------------------------------------------------------- #
# the table
# --------------------------------------------------------------------------- #

PROFILES: dict[str, VendorProfile] = {
    # ---- DeepSeek: the cheapest cache in the market, plus time-of-day pricing
    "deepseek": VendorProfile(
        id="deepseek", label="DeepSeek", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.02, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="disk",
        batch_discount=1.0, supports_flex=False, peak_valley=True,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,   # automatic; a marker would be ignored noise
        usage_aliases={
            "prompt_cache_hit_tokens": "cached_tokens",
            "prompt_cache_miss_tokens": "_uncached_tokens",
        },
        notes="硬盘缓存默认开启，命中价为未命中的 2%（flash）/3.3%（v4-pro）。"
              "高峰为 UTC 周一至周五 01–04、06–10，其余时段全部五折。",
    ),

    # ---- Anthropic: the only vendor where the marker is mandatory
    "anthropic": VendorProfile(
        id="anthropic", label="Anthropic", wire="anthropic",
        cache_mode="explicit_anthropic",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="5m",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=True,
        usage_aliases={},
        notes="必须显式打 cache_control 标记才会缓存；5 分钟档写入 1.25×、"
              "命中 0.10×；1 小时档写入 2×。Opus 5.5 命中 0.05×、Fable 5.1 命中 0.025×。",
    ),

    # ---- OpenAI: automatic, with an optional explicit mode on newer models
    "openai": VendorProfile(
        id="openai", label="OpenAI", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="30m",
        batch_discount=0.5, supports_flex=True, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=2.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        notes="默认自动缓存（读取 0.1×，GPT-6.1 Sol 为 0.05×）。"
              "short/long 两档上下文价；Flex 档约 5 折，Fast 档 2×、Ultrafast 6×。",
    ),

    # ---- Gemini: implicit only, and explicit caching lives on another API
    "gemini": VendorProfile(
        id="gemini", label="Google Gemini", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.0,
        min_cache_tokens=4096, cache_ttl="managed",
        batch_discount=0.5, supports_flex=True, peak_valley=False,
        long_context_threshold=200_000, long_context_multiplier=2.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={"total_cached_tokens": "cached_tokens",
                       "cachedContentTokenCount": "cached_tokens"},
        notes="隐式缓存默认开启，最小前缀 4096（3.x）/2048（2.5）。"
              "超 200K 输入输出同时翻倍；显式缓存另收存储费（按 1M·小时）。",
    ),

    # ---- Alibaba Bailian: both modes, explicit needs a marker on messages
    "bailian": VendorProfile(
        id="bailian", label="阿里云百炼", wire="openai",
        cache_mode="explicit_bailian",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="5m",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,   # opt-in: explicit and implicit are mutually exclusive
        usage_aliases={"prompt_tokens_details.cached_tokens": "cached_tokens",
                       "input_tokens_details.cached_tokens": "cached_tokens"},
        notes="显式缓存（标记 1.25× 写、0.10× 读）与隐式缓存（0.20× 读）互斥，"
              "单请求只能二选一。工具定义参与缓存，顺序与字段必须逐字节一致。",
    ),

    # ---- Zhipu GLM: implicit, cheapest among the domestic automatic providers
    "zhipu": VendorProfile(
        id="zhipu", label="智谱 GLM", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.25, cache_write_multiplier=1.0,
        min_cache_tokens=512, cache_ttl="managed",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        notes="隐式缓存自动识别，最小前缀 512。GLM-5.3 命中 25%。"
              "思考强度分 low/high/max 三档，思考 token 按输出价计费。",
    ),

    # ---- Kimi: explicit, and the only one that bills cache storage by time
    "kimi": VendorProfile(
        id="kimi", label="Kimi 月之暗面", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="5m",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        notes="重复前缀自动缓存，分 5min / 1h 两档 TTL。k3 缓存写入单独计费"
              "（¥20 / 1M，1h 档 ¥40）。",
    ),

    # ---- MiMo (Xiaomi): deepest cache discount in the fleet, on paper
    # 2026-10-07 联网核实：MiMo-V2.5-Pro 官方口径为输入（命中缓存）¥0.025/M、
    # 输入（未命中）¥3/M、输出 ¥6/M → 命中倍率 0.025/3 ≈ 0.83%，
    # 比 DeepSeek 的 2% 还低。多家媒体（腾讯新闻/搜狐/cnblogs，9/22–9/27）
    # 报道 MiMo-V2.6 系列沿用 V2.5 定价，故此处按同一口径填入。
    #
    # 注意与 EFFECTIVE_RATES 的区别：pricing.EFFECTIVE_RATES 里 mimo 的
    # ¥0.0754/M 是从账单反推的**有效混合单价**，其中含 Token Plan 订阅积分的
    # 折算，远低于目录价。两者回答不同问题——此处给倍率（尺度无关），
    # 价格仍走 EFFECTIVE_RATES，于是决策对、金额也贴合实际付费。
    "mimo": VendorProfile(
        id="mimo", label="小米 MiMo", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.025 / 3.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        notes="命中倍率 0.83%（¥0.025 vs 未命中 ¥3），全车队最低，比 DeepSeek 的 2% 还深。"
              "官方未公布最小前缀与 TTL，这里按保守 3 小时估计。"
              "另有低谷优惠：00:00–08:00（UTC+8）按 0.8× 计。",
    ),

    # ---- MiniMax: implicit cache, discount varies by model generation
    "minimax": VendorProfile(
        id="minimax", label="MiniMax", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.20, cache_write_multiplier=1.0,
        min_cache_tokens=512, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        notes="隐式缓存，命中倍率随代际浮动（M3 / M2.7 约 20%，M2.5 / M2.1 约 10%，"
              "取自阿里云百炼托管口径）。这里取 20% 偏保守，避免高估节省。",
    ),

    # ---- Volcengine / Doubao
    # 2026-10-07 联网核实：火山方舟确实有「缓存命中」计费项——
    # doubao-seed-evolving 的在线推理（常规）缓存命中单价已启用（第三方监测站
    # 2026-09-22 记录到该行存在）；豆包 2.1 Pro 的已公布比率为命中 ¥1.2 vs
    # 未命中 ¥6 = 20%。但 **seed-evolving 自己的比率与最小前缀未公开**，
    # 所以这里仍不填具体倍率：把 2.1 Pro 的 20% 套到另一个模型上就是编造。
    "volcengine": VendorProfile(
        id="volcengine", label="火山方舟（豆包）", wire="openai",
        cache_mode="unknown",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="unknown",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        notes="缓存命中计费确实存在（seed-evolving 已有该项，豆包 2.1 Pro 的"
              "已公布比率为 20%），但 seed-evolving 自身比率与最小前缀未公开，"
              "故不代填——需人工填入 vendor 行后才会参与决策。",
    ),

    # ---- StepFun
    "stepfun": VendorProfile(
        id="stepfun", label="阶跃星辰 StepFun", wire="openai",
        cache_mode="unknown",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="unknown",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        notes="百炼托管的 step 系列在隐式缓存名单内，但未公布折扣比例，按原价估算。",
    ),

    # ---- local engines: no billing, but the same interface
    "local": VendorProfile(
        id="local", label="本地推理引擎", wire="openai",
        cache_mode="none",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="none",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=False,
        inject_cache_control=False,
        notes="本地引擎不按 token 计费，缓存折扣不适用。适配重点是多轮工具调用的"
              "历史重放（走 OpenAI 兼容端点）。",
    ),

    # ---- fallback: behave like a plain OpenAI-compatible endpoint
    "generic": VendorProfile(
        id="generic", label="未识别端点", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=1024, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=False,
        inject_cache_control=False,
        notes="未识别的端点：保守处理，不注入任何厂商专属字段，按原价估算。",
    ),
}

# Vendor ids in the order the setup wizard offers them.
VENDOR_ORDER: tuple[str, ...] = (
    "deepseek", "anthropic", "openai", "gemini", "bailian", "zhipu", "kimi",
    "mimo", "minimax", "volcengine", "stepfun", "local",
)

# Substrings that identify a vendor from a base URL.
# 2026-10-07: 改成「主机名 + 域名后缀」匹配，不再对整条 URL 做子串匹配。
# 旧写法有两个真问题：
#   ① https://api.openai.com.attacker.example/v1 会被当成 OpenAI（子串命中）
#   ② https://other.example/api.openai.com/v1 也会被当成 OpenAI（路径里带域名）
# 域名归属必须看 host，不能看 path，也不能看 host 的后缀区。
_VENDOR_DOMAINS: dict[str, tuple[str, ...]] = {
    "deepseek": ("api.deepseek.com", "deepseek.com"),
    "anthropic": ("api.anthropic.com", "anthropic.com"),
    "openai": ("api.openai.com", "openai.com", "openai.azure.com"),
    "gemini": ("generativelanguage.googleapis.com", "ai.google.dev", "googleapis.com"),
    "bailian": ("dashscope.aliyuncs.com", "aliyuncs.com", "bailian.console.aliyun.com"),
    "zhipu": ("open.bigmodel.cn", "bigmodel.cn", "zhipu.ai"),
    "kimi": ("api.moonshot.cn", "moonshot.cn", "moonshot.ai"),
    "mimo": ("api.xiaomimimo.com", "xiaomimimo.com", "mimo.xiaomi.com"),
    "minimax": ("api.minimaxi.com", "minimaxi.com", "minimax.chat", "minimax.io"),
    "volcengine": ("ark.cn-beijing.volces.com", "volces.com", "volcengine.com"),
    "stepfun": ("api.stepfun.com", "stepfun.com", "stepfun.ai"),
}

# 环境标识：环回/内网地址不可能属于任何云厂商。
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")


def _host_of(url: str) -> str:
    """提取纯主机名（小写，去掉端口与用户信息）。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw if "//" in raw else "//" + raw)
    return (parsed.hostname or "").lower()


def _host_matches(host: str, domain: str) -> bool:
    """host 等于该域名，或者是它的真子域（点边界），防止后缀欺骗。"""
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)

# Substrings that identify a vendor from a model name.
_MODEL_HINTS: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek",),
    "anthropic": ("claude",),
    "openai": ("gpt-", "o1", "o3", "o4", "chatgpt"),
    "gemini": ("gemini", "gemma"),
    "bailian": ("qwen", "wan"),
    "zhipu": ("glm", "cogview", "cogvideo", "codegeex", "autoglm"),
    "kimi": ("kimi", "moonshot"),
    "mimo": ("mimo",),
    "minimax": ("minimax", "abab"),
    "volcengine": ("doubao", "seed-"),
    "stepfun": ("step-", "stepfun"),
}

_LOCAL_SERVICES = {"ollama", "llamacpp", "mnn"}


def profile_for(
    *, base_url: str = "", model: str = "", service: str = "",
    vendor: str = "", wire: str = "",
) -> VendorProfile:
    """Resolve the profile for a provider row.

    Precedence: explicit ``vendor`` > local ``service`` > hostname > model name
    > wire default > generic. Explicit always wins so a user can force a
    profile for an aggregator that fronts several vendors.

    ``model`` should be the model **actually being called**, not the provider's
    declared default — an aggregator row that fronts several vendors must be
    judged by the model on this request, otherwise every call through it is
    priced as whichever vendor happens to sit in the default field.
    """
    if vendor and vendor in PROFILES:
        return PROFILES[vendor]
    if service and service.lower() in _LOCAL_SERVICES:
        return PROFILES["local"]

    host = _host_of(base_url)
    if host and not any(host == h for h in _LOCAL_HOSTS):
        for vid, domains in _VENDOR_DOMAINS.items():
            if any(_host_matches(host, domain) for domain in domains):
                return PROFILES[vid]

    haystack_model = (model or "").lower()
    for vid, hints in _MODEL_HINTS.items():
        if any(hint in haystack_model for hint in hints):
            return PROFILES[vid]

    if wire == "anthropic":
        # An anthropic-wire endpoint we could not identify is most likely a
        # translation gateway; the conservative generic profile is safer than
        # assuming the real Anthropic billing rules apply.
        return PROFILES["generic"]
    return PROFILES["generic"]


# --------------------------------------------------------------------------- #
# peak / valley
# --------------------------------------------------------------------------- #

# DeepSeek: peak = UTC Mon–Fri 01:00–04:00 and 06:00–10:00.
# Chinese public holidays are off-peak in full; we cannot compute the holiday
# calendar offline, so we expose the flag and let the caller pass a holiday set.
_DEEPSEEK_PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak(profile: VendorProfile, when: float | None = None,
            *, holiday: bool = False) -> bool:
    """True when ``when`` (epoch seconds) falls in a vendor's expensive window."""
    if not profile.peak_valley:
        return False
    if holiday:
        return False
    stamp = time.gmtime(when if when is not None else time.time())
    if stamp.tm_wday >= 5:            # Saturday / Sunday
        return False
    hour = stamp.tm_hour
    return any(start <= hour < end for start, end in _DEEPSEEK_PEAK_WINDOWS_UTC)


def price_multiplier(profile: VendorProfile, *, when: float | None = None,
                     holiday: bool = False, batch: bool = False,
                     flex: bool = False, prompt_tokens: int = 0) -> float:
    """Combined multiplier from the three *usage-shape* dimensions.

    Deliberately excludes the cache multipliers: those apply per token class
    rather than to the whole request, and mixing them here would make the
    result meaningless. This is the "same request, different circumstances"
    factor, which is the one a scheduler can actually act on.
    """
    factor = 1.0
    if is_peak(profile, when, holiday=holiday):
        factor *= 2.0
    if batch and profile.batch_discount < 1.0:
        factor *= profile.batch_discount
    if flex and profile.supports_flex:
        factor *= 0.5
    if profile.long_context_threshold and prompt_tokens > profile.long_context_threshold:
        factor *= profile.long_context_multiplier
    return factor


# --------------------------------------------------------------------------- #
# usage normalisation
# --------------------------------------------------------------------------- #


def _flatten(node: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten a nested usage object into dotted keys (bounded depth)."""
    out: dict[str, Any] = {}
    if depth > 3:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            out[dotted] = value
            if isinstance(value, dict):
                out.update(_flatten(value, dotted, depth + 1))
    return out


def _usable_count(value: Any) -> int | None:
    """有限非负整数，否则 None。

    Usage 字段是外部输入（上游响应），可能是 inf / nan / 负数：
    int(inf) 会抛 OverflowError，int(nan) 抛 ValueError，负数会把
    命中率与成本算成负的。统一在这里丢成 0，而不是让一次脏响应
    把整条请求炸掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return int(value)


def normalize_usage(profile: VendorProfile, raw: dict[str, Any] | None) -> dict[str, int]:
    """Map a vendor's ``usage`` object onto the canonical key set.

    Two passes: an exact (dotted) match first, then a leaf-name fallback so
    nested shapes work without every vendor needing a hand-written path.
    ``prompt_tokens_details.cached_tokens`` resolves in pass 1 for Bailian
    (which declares the full path) and in pass 2 for plain OpenAI (leaf =
    ``cached_tokens``). Unknown fields are ignored; malformed values
    (infinite / NaN / negative) are dropped rather than propagated.
    """
    aliases = {**_BASE_USAGE_ALIASES, **profile.usage_aliases}
    flat = _flatten(raw or {})
    out: dict[str, int] = {}

    # Pass 1 — exact dotted matches (most specific wins).
    for key, value in flat.items():
        canonical = aliases.get(key)
        if canonical:
            count = _usable_count(value)
            if count is not None:
                out[canonical] = count

    # Pass 2 — leaf-name fallback for anything still unresolved.
    for key, value in flat.items():
        leaf = key.rsplit(".", 1)[-1]
        canonical = aliases.get(leaf)
        if canonical and canonical not in out:
            count = _usable_count(value)
            if count is not None:
                out[canonical] = count

    # DeepSeek reports hit and miss separately; total input is the sum.
    if "_uncached_tokens" in out:
        miss = out.pop("_uncached_tokens")
        hits = out.get("cached_tokens", 0)
        if "input_tokens" not in out:
            out["input_tokens"] = miss + hits

    return out


def hit_rate(usage: dict[str, int]) -> float:
    """Fraction of prompt tokens served from cache (0.0–1.0)."""
    total = int(usage.get("input_tokens", 0))
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, int(usage.get("cached_tokens", 0)) / total))


# --------------------------------------------------------------------------- #
# request shaping
# --------------------------------------------------------------------------- #


def should_inject_cache(profile: VendorProfile, requested: str = "") -> bool:
    """Decide whether to put a cache marker on this request.

    ``requested`` comes from provider config: ``"auto"`` (profile decides),
    ``"off"`` (never), ``"explicit"`` (force on).
    """
    mode = (requested or "auto").strip().lower()
    if mode in ("off", "none", "false", "0"):
        return False
    if mode in ("explicit", "on", "true", "1"):
        return True
    return profile.inject_cache_control


def shape_anthropic(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    ttl: str = "",
    max_breakpoints: int = 4,
) -> dict[str, Any]:
    """Attach ``cache_control`` to the stable prefix of an Anthropic request.

    Two breakpoints are used, one per cacheable *layer* rather than one per
    block: tools first (they change the least), then the system prompt. That
    matches the vendor's own hierarchy (``tools`` → ``system`` → ``messages``)
    so a change to the system prompt does not invalidate the tools entry.

    The marker is placed on the **last** element of each layer — the marker
    means "cache everything up to and including this block", so putting it on
    the last tool and the last system block covers both layers in full.

    2026-10-07 修正：去掉“工具数超过 32 就不打标记”的旧上限。那一行会让
    工具很多的 agent（真实场景常见）彻底拿不到工具层缓存，而断点数量与
    工具个数无关——永远只需最后一个。

    Returns the payload, mutated in place for convenience.
    """
    marker: dict[str, Any] = {"type": "ephemeral"}
    if ttl in ("1h", "3600"):
        marker["ttl"] = "1h"

    # -- tools ---------------------------------------------------------------
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        last = tools[-1]
        if isinstance(last, dict):
            last["cache_control"] = dict(marker)

    # -- system --------------------------------------------------------------
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": dict(marker)}]
    elif isinstance(system, list) and system:
        last = system[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = dict(marker)

    return payload


def shape_bailian(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    max_breakpoints: int = 4,
) -> dict[str, Any]:
    """Attach a Bailian-style explicit marker to the last **stable** block.

    2026-10-07 修正：旧实现打在最后一条消息上，那是每轮都变的用户输入——
    结果每次都写一份新缓存、永远命中不了上一次，比不开缓存还贵。
    稳定块应该是系统消息（system 角色），它跨请求不变、且工具定义会作为
    系统消息的一部分参与缓存。没有系统消息时不动（宁可不缓存，不误伤）。

    Bailian（DashScope）在 OpenAI 兼容面上接受把 Anthropic 风格的
    ``cache_control`` 放在 message 的 content 块里；工具定义不能单独挂标记。
    本行为是 opt-in（profile 的 ``inject_cache_control`` 为 False），
    因为显式与隐式缓存在该家互斥，而隐式模式本来就默认开着且免费。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload

    target = None
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            target = message
    if target is None:
        return payload

    marker = {"type": "ephemeral"}
    content = target.get("content")
    if isinstance(content, str):
        if not content.strip():
            return payload
        target["content"] = [{"type": "text", "text": content, "cache_control": dict(marker)}]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = dict(marker)
    return payload


def shape_request(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    cache_control: str = "auto",
    ttl: str = "",
) -> dict[str, Any]:
    """Apply the vendor's request adaptation. No-op when the marker is off."""
    if not should_inject_cache(profile, cache_control):
        return payload
    if profile.cache_mode == "explicit_anthropic":
        return shape_anthropic(payload, profile, ttl=ttl)
    if profile.cache_mode == "explicit_bailian":
        return shape_bailian(payload, profile)
    return payload


# --------------------------------------------------------------------------- #
# mixed-fleet helpers
# --------------------------------------------------------------------------- #


def fleet_cache_compatible(profiles: Iterable[VendorProfile]) -> bool:
    """Legacy vendor-homogeneity flag, retained for compatibility.

    This does not prove cache sharing: endpoint, account, model, wire, cache
    mode and exact prefix still isolate namespaces. Request-level telemetry
    in CacheWarmth, not this fleet summary, drives warmth decisions.
    """
    ids = {p.id for p in profiles}
    if ids <= {"local"}:
        return True
    if len(ids - {"local"}) > 1:
        return False
    return True


def describe_fleet(profiles: Iterable[VendorProfile]) -> dict[str, Any]:
    """Summary for the selection UI and for ``forge vendors``."""
    rows = list(profiles)
    ids = [p.id for p in rows]
    unique = list(dict.fromkeys(ids))
    best_cache = min((p.cache_read_multiplier for p in rows), default=1.0)
    return {
        "vendors": unique,
        "vendor_count": len([v for v in unique if v != "local"]),
        "mode": "single" if len([v for v in unique if v != "local"]) <= 1 else "mixed",
        "cache_shared": fleet_cache_compatible(rows),
        "cross_model_cache_guaranteed": False,
        "cache_namespace": ["endpoint", "account", "model", "wire", "cache_mode", "prefix"],
        "best_cache_read_multiplier": best_cache,
        "any_peak_valley": any(p.peak_valley for p in rows),
        "any_long_context_cliff": any(p.long_context_threshold for p in rows),
        "any_explicit_cache": any(p.explicit_cache for p in rows),
        "live_notes": [f"{p.label}: {p.notes}" for p in rows],
    }


__all__ = [
    "CANONICAL_USAGE_KEYS",
    "PROFILES",
    "VENDOR_ORDER",
    "VendorProfile",
    "describe_fleet",
    "fleet_cache_compatible",
    "hit_rate",
    "is_peak",
    "normalize_usage",
    "price_multiplier",
    "profile_for",
    "shape_anthropic",
    "shape_bailian",
    "shape_request",
    "should_inject_cache",
]
