"""Model supply: provider registry + tiered reliability.

OpenCode contributes the provider abstraction (one wire format, providers
declared as data, a separate cheap ``small_model`` for background chores).
Hermes contributes the tiering above it:

    primary  ->  fallback chain (by error class)  ->  optional MoA aggregate

Only *retryable* failures advance the chain. A 400 from a bad request is a bug
in our prompt, not a reason to burn three more providers.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import time

from .local_service import chat_request_path
from .tool_adapter import fill_gemini_name_fields, sanitize_messages
from .adapters import PlanContext, adapter_for
from .cache_state import CacheWarmth, prefix_fingerprint
from .vendors import (
    VendorProfile,
    normalize_usage,
    profile_for,
)
from . import sampling
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


# -- cache warmth store ------------------------------------------------------
# 进程级单例：记录「哪个前缀在哪家厂商仍然是热的」。缓存隔离是按厂商的，
# 混排下这个信念决定了升级跳要不要真的执行。惰性创建，不碰文件系统除非用到。
_WARMTH: CacheWarmth | None = None


def warmth_store() -> CacheWarmth:
    global _WARMTH
    if _WARMTH is None:
        from pathlib import Path

        home = Path(os.environ.get("FORGE_HOME") or (Path.home() / ".forge"))
        _WARMTH = CacheWarmth(home / "cache-warmth.json")
    return _WARMTH


class TransportError(RuntimeError):
    def __init__(self, message, *args):
        from .secrets import redact
        super().__init__(redact(str(message)), *args)
    retryable = True


class RateLimited(TransportError):
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after  # seconds, parsed from Retry-After header


class Overloaded(TransportError):
    retryable = True


class BadRequest(TransportError):
    retryable = False


def _as_temperature(raw: Any) -> float | None:
    """配置里的 temperature 容错转 float；空/非法值一律当作「不设置」。"""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 2.0 else None


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = ""
    wire: str = "openai"          # openai | anthropic
    models: tuple[str, ...] = ()
    default_model: str = ""
    small_model: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    rpm: int = 0                    # >0 enables proactive throttling (requests/min)
    # 该 provider 的采样温度（来自配置行）。None = 不发送，走服务端默认。
    # 最终发不发还要过 sampling 策略：Claude 协议与只认默认值的模型一律不发。
    temperature: float | None = None
    # Self-hosted engine id (ollama | llamacpp | mnn), or "" for a cloud
    # provider. Gates the local-only adaptations in local_service.py; an
    # unrecognised value stays "" so a typo can never reroute a cloud provider.
    service: str = ""
    # 厂商适配：显式 vendor id（"" = 按 baseURL/model/service 自动识别）。
    # 适配层决定要不要打 cache_control、命中价怎么算、是否处在峰谷时段，
    # 见 vendors.py。留空不影响任何既有 provider 的行为。
    vendor: str = ""
    # 缓存标记策略：auto（按厂商画像推荐）/ off（从不注入）/ explicit（强制注入）。
    cache_control: str = "auto"
    # 预期这条前缀还会被复用几次——适配器用它算缓存回本点。
    # 默认 2（保守的“还有一次”），设 1 等于宣告本次不走缓存。
    expected_calls: int = 2
    # 典型调用间隔（秒），用于选 5m / 1h TTL。
    call_gap_seconds: float = 60.0
    # 是否启用适配器规划（关掉则退回到不注入任何厂商专属字段）。
    adapt: bool = True
    think_budget: int | None = None
    flex: bool = False
    defer: bool = False
    max_defer_seconds: float = 0
    _credential: Any = field(default=None, repr=False, compare=False)
    _secret_scope: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        from .secrets import SecretScope, VendorCredential
        self._secret_scope = SecretScope()
        from urllib.parse import urlsplit, parse_qsl
        from .secrets import secret_field
        parts = urlsplit(self.base_url)
        if parts.username is not None or any(secret_field(k) for k, _ in parse_qsl(parts.query)):
            raise BadRequest('Credentials embedded in provider URLs are unsupported; use protected authentication fields')
        self._credential = self._credential or VendorCredential(str(self.api_key), self.base_url)
        self.api_key = self._credential.ref
        # Custom authentication headers also belong to the trusted boundary.
        self._header_credentials = {}
        for name, value in self.headers.items():
            if secret_field(name):
                self._header_credentials[name] = VendorCredential(str(value), self.base_url)
        self.headers = {k: (self._header_credentials[k].ref if k in self._header_credentials else v)
                        for k, v in self.headers.items()}

    def profile(self) -> VendorProfile:
        """该 provider 的厂商计费画像（按声明的默认模型识别）。"""
        return profile_for(
            base_url=self.base_url,
            model=self.default_model or (self.models[0] if self.models else ""),
            service=self.service,
            vendor=self.vendor,
            wire=self.wire,
        )

    def profile_for_model(self, model: str) -> VendorProfile:
        """按**本次实际调用的模型**识别厂商。

        聚合器行（一个 baseURL 后面挂多家厂商）必须用实际模型判定，
        否则整条链路上的请求都会被算成默认字段里那一家。
        """
        return profile_for(
            base_url=self.base_url,
            model=model or self.default_model,
            service=self.service,
            vendor=self.vendor,
            wire=self.wire,
        )

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def chat_url(self) -> str:
        """URL for chat + tool-loop requests, service-gated.

        Cloud providers keep the historical ``/chat/completions``; the three
        self-hosted engines get the OpenAI-compatible surface, which is the
        only one that accepts a replayed tool-call history.
        """
        return self.url(chat_request_path(self.service, self.wire, self.base_url))

    def auth_headers(self) -> dict[str, str]:
        """Public inspection returns opaque references, never credentials."""
        if self.wire == "anthropic":
            return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", **self.headers}
        return {"Authorization": f"Bearer {self.api_key}", **self.headers}

    def _execution_headers(self, url: str) -> dict[str, str]:
        headers = {**self._credential._header(url, wire=self.wire), **self.headers}
        for name, credential in self._header_credentials.items():
            headers[name] = credential._header(url, wire='anthropic')['x-api-key']
        return headers


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    provider: str = ""
    # 缓存与思考：计费的三类额外 token（vendors.normalize_usage 归一后填入）。
    # 默认 0，老的调用点不传也完全兼容。
    cached_tokens: int = 0          # 命中缓存读到的输入 token
    cache_write_tokens: int = 0     # 写入缓存的输入 token
    reasoning_tokens: int = 0       # 思考 token（多数厂商按输出价计费）

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def uncached_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_tokens)

    @property
    def hit_rate(self) -> float:
        """命中率 = 命中 token / 全部输入 token（0.0–1.0）。"""
        if self.prompt_tokens <= 0:
            return 0.0
        return max(0.0, min(1.0, self.cached_tokens / self.prompt_tokens))


@dataclass
class Completion:
    text: str
    usage: Usage
    attempts: list[dict[str, Any]] = field(default_factory=list)
    aggregated: bool = False
    candidates: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    wire: str = "openai"
    assistant_message: dict[str, Any] | None = None
    # 本次请求的适配决策（adapters.RequestPlan.to_raw()）。
    # 上游 transport 把它放进 extra["plan"]，路由层透传到 Completion，
    # 于是运行报告能解释"这条请求为什么这么发"。
    plan: dict[str, Any] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if not self.requests:
            self.requests = [{"provider": self.usage.provider, "model": self.usage.model,
                              "usage": dict(vars(self.usage)), "plan": copy.deepcopy(self.plan),
                              "estimate_scope": "reuse_window", "invoice_verified": False}]


def merge_completions(final: Completion, stages: list[tuple[str, Completion]]) -> Completion:
    """Keep each billed request with its own model, usage and plan.

    Reuse-window estimates overlap and cannot be added into an invoice.
    The legacy vendor field remains available for older report consumers.
    """
    requests = []
    for stage, completion in stages:
        for request in completion.requests:
            requests.append({**copy.deepcopy(request), "stage": stage})
    final.requests = requests
    counts = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")
    final.usage = Usage(model=final.usage.model, provider=final.usage.provider,
                        **{k: sum(_safe_count(r["usage"].get(k)) for r in requests) for k in counts})
    final.plan = {**final.plan, "scope": "multi_request", "stages": copy.deepcopy(requests),
                  "invoice_verified": False, "estimate_scope": "per_stage_reuse_window"}
    return final


def _safe_count(value: Any) -> int:
    """有限非负整数，否则 0。

    usage 来自上游响应，是外部输入：``inf`` / ``nan`` / 负数都可能出现。
    ``int(inf)`` 抛 OverflowError、``int(nan)`` 抛 ValueError，而负计数会把
    命中率与成本算成负数。统一在这里退成 0，别让一次脏响应把整条请求炸掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    if value < 0:
        return 0
    return int(value)


def _plan_and_shape(payload: dict[str, Any], messages: list[dict[str, Any]],
                    provider: "Provider", model: str, *, store=None,
                    **options) -> tuple[dict[str, Any], dict[str, Any]]:
    """让厂商适配器决定这条请求怎么发，并把决定落到 payload 上。

    返回 (payload, plan_raw)。适配器算不出来（模型无价）时退化到不注入，
    行为与改造前一致。

    两条纪律：
    * 画像按**实际调用的模型**识别——聚合器不能拿默认模型当整条链路的厂商；
    * **不修改调用方传入的 payload**：整形前深拷贝，否则重试/回退路径
      会拿到一份已经被改过一次的请求体，标记会叠加。
    """
    profile = provider.profile_for_model(model)
    if options.get("flex", provider.flex) and provider.wire != "openai":
        raise ValueError("Flex requires an OpenAI-compatible wire")
    from .request_execution import prepare_execution
    think_budget = options.get("think_budget", provider.think_budget)
    payload, execution = prepare_execution(
        payload, profile, model, interactive=options.get("interactive", True),
        think_budget=think_budget, flex=options.get("flex", provider.flex),
        defer=options.get("defer", provider.defer),
        max_defer_seconds=options.get("max_defer_seconds", provider.max_defer_seconds),
        batch=options.get("batch", False), batch_submission=options.get("batch_submission", False),
        when=options.get("when"))
    if not provider.adapt:
        return payload, {"vendor": profile.id, "actions": ["适配器已关闭"],
                         "cache_engages": False, "execution": execution, "model": model,
                         "estimate_scope": "reuse_window", "includes_output_cost": False}

    from .pricing import index_price

    base_in, base_out, price_known = index_price(model)

    fingerprint = prefix_fingerprint(payload, model=model,
                                     endpoint=provider.base_url,
                                     account=provider._credential._account_fingerprint, wire=provider.wire,
                                     cache_mode=provider.cache_control)
    ctx = PlanContext(
        profile=profile, model=model,
        base_input=float(base_in or 0.0), base_output=float(base_out or 0.0),
        calls_expected=max(1, int(provider.expected_calls)),
        gap_seconds=float(provider.call_gap_seconds),
        warm=(store or warmth_store()).is_warm(profile, fingerprint),
        wire=provider.wire,
        cache_control=provider.cache_control,
        interactive=options.get("interactive", True), think_budget=think_budget,
        flex=bool(execution.get("flex")), batch=bool(execution.get("batch")),
        when=options.get("when"),
    )
    plan = adapter_for(profile).plan(payload, messages, ctx)

    # 把计划落到实际请求体：只有需要显式标记的厂商才注入，其余保持原样。
    if plan.cache_control:
        from .vendors import shape_anthropic, shape_bailian

        shaped = copy.deepcopy(payload)
        if profile.cache_mode == "explicit_anthropic":
            shaped = shape_anthropic(shaped, profile, ttl=plan.ttl)
        elif profile.cache_mode == "explicit_bailian":
            shaped = shape_bailian(shaped, profile)
        payload = shaped

    raw = plan.to_raw()
    raw["fingerprint"] = fingerprint
    raw["execution"] = execution
    raw["estimate_scope"] = "reuse_window"
    raw["includes_output_cost"] = False
    raw["price_known"] = price_known
    raw["model"] = model
    if plan.reasoning_budget and "reasoning" not in execution:
        execution["reasoning"] = {"status": "suggestion", "requested": plan.reasoning_budget}
    if plan.defer and "defer" not in execution:
        execution["defer"] = {"status": "suggestion", "delay_seconds": 0}
    if not price_known:
        # 金额绝对值不可信（模型不在目录与账单反推表里），但决策比例仍然成立。
        raw.setdefault("notes", []).append(
            "该模型无目录价，金额按单位价折算，仅用于比较比例，不代表真实账单")
    return payload, raw


def _remember_warmth(provider: "Provider", plan_raw: dict[str, Any],
                     usage: Any = None, *, store=None) -> None:
    """记下这次调用之后，该前缀在该厂商变热了（供后续跳转决策用）。

    只有响应里真的报了 cache_write 或 cached，才确认该前缀有缓存。
    自动缓存首轮 cached=0 不代表失败，也不代表已确认命中；宁可漏记。

    ``usage`` 可缺省（无 telemetry 时），也可传 ``Usage`` 或普通 dict。
    """
    fingerprint = str(plan_raw.get("fingerprint") or "")
    if not fingerprint:
        return
    profile = provider.profile_for_model(str(plan_raw.get("model") or ""))
    from .vendors import PROFILES
    profile = PROFILES.get(plan_raw.get("vendor"), profile)
    if profile.cache_mode in ("none", "unknown") or not plan_raw.get("cache_engages"):
        return
    if isinstance(usage, dict):
        write = _safe_count(usage.get("cache_write_tokens", 0))
        cached = _safe_count(usage.get("cached_tokens", 0))
    elif usage is not None:
        write = _safe_count(getattr(usage, "cache_write_tokens", 0))
        cached = _safe_count(getattr(usage, "cached_tokens", 0))
    else:
        write = cached = 0
    if not (write or cached):
        return
    (store or warmth_store()).remember(profile.id, fingerprint)


class Transport(Protocol):
    def complete(self, provider: Provider, model: str, messages: list[dict[str, Any]],
                 **options: Any) -> tuple[str, Usage]: ...


class _ProviderRateLimiter:
    """Proactive per-provider throttle: slots reserved 60/rpm s apart.

    Reservation happens under a lock; the sleep happens outside it so
    concurrent callers queue behind each other's slots instead of piling
    onto the same instant. rpm<=0 (default) means no throttling.
    """

    def __init__(self) -> None:
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, provider: "Provider") -> float:
        """Reserve the next slot; returns seconds the caller must wait."""
        if provider.rpm <= 0:
            return 0.0
        interval = 60.0 / provider.rpm
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(provider.name, 0.0) + interval)
            self._next_slot[provider.name] = slot
            return max(0.0, slot - now)


class HttpTransport:
    """Minimal stdlib transport for OpenAI- and Anthropic-shaped endpoints."""

    def __init__(self, timeout: int = 120, *, response_opener=None) -> None:
        self.timeout = timeout
        self._response_opener = response_opener  # trusted host injection, never a model option
        self._limiter = _ProviderRateLimiter()

    def complete(self, provider: Provider, model: str, messages: list[dict[str, Any]],
                 **options: Any) -> tuple[str, Usage]:
        from .secrets import SecretScope
        scope = options.pop('secret_scope', None) or SecretScope()
        messages = scope.protect(messages)
        options = scope.protect(options)
        # Proactive throttle BEFORE the wire: providers with rpm>0 (e.g.
        # StepFun free tier at 10 RPM) get slot-reserved so bursts die at
        # the 429 stage far less often. No-op for rpm<=0 providers.
        wait = self._limiter.reserve(provider)
        if wait > 0:
            time.sleep(wait)
        # 发送前最后防线：按 wire 协议清洗消息序列（配对 tool_use/tool_result、
        # 剔除孤儿 tool 消息、空 content 补占位），避免各家 API 的 400。
        messages = sanitize_messages(messages, provider.wire)
        # Gemini 兼容网关要求每条 tool 消息带 name（hermes-agent #16478）
        if "gemini" in model.lower():
            messages = fill_gemini_name_fields(messages)
        if provider.wire == "anthropic":
            system = "\n".join(str(m["content"]) for m in messages if m.get("role") == "system")
            chat = [m for m in messages if m.get("role") != "system"]
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": int(options.get("max_tokens", 2048)),
                "messages": chat,
            }
            if system:
                payload["system"] = system
            if options.get("tools"):
                payload["tools"] = options["tools"]
            # Claude 协议只认 model / max_tokens / messages / system / tools：
            # 采样参数一律摘掉（带上去会 400）。
            sampling.strip_extra_params(payload, model, provider.wire)
            url = provider.url("/v1/messages")
        else:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": int(options.get("max_tokens", 2048)),
            }
            requested = options.get("temperature")
            if requested is None:
                requested = provider.temperature
            effective = sampling.resolve(model, provider.wire, requested)
            if effective is not None:
                payload["temperature"] = effective
            if options.get("tools"):
                payload["tools"] = options["tools"]
            sampling.strip_extra_params(payload, model, provider.wire)
            # Service-gated: local engines (ollama / llamacpp / mnn) need the
            # OpenAI-compatible path for tool-call replay; cloud unchanged.
            url = provider.url(chat_request_path(provider.service, provider.wire,
                                                provider.base_url))

        for key in ("reasoning_effort", "thinking", "thinking_budget", "enable_thinking", "service_tier", "tool_choice"):
            if key in options:
                payload[key] = copy.deepcopy(options[key])
        if "reasoning_effort" in payload and "thinking_budget" in payload:
            raise BadRequest("reasoning_effort and thinking_budget cannot be combined")
        # 厂商适配器规划：适配器（adapters.py）不只看「要不要打标记」，
        # 而是枚举该厂商所有合法方案（缓存/不缓存、5m/1h TTL、单/双断点），
        # 用预期复用次数算出每个方案的成本，选最便宜的那个。
        # 返回的 plan 含成本、节省额与人类可读的动作，供运行报告审计。
        profile = provider.profile_for_model(model)
        try:
            options.setdefault("interactive", False)
            payload, plan_raw = _plan_and_shape(payload, messages, provider, model, **options)
            from .request_execution import wait_for_schedule
            wait_for_schedule(plan_raw, options.get("cancel_event"))
            if plan_raw.get("execution", {}).get("defer", {}).get("delay_seconds"):
                payload, plan_raw = _plan_and_shape(payload, messages, provider, model, **options)
                plan_raw["execution"]["defer"]["status"] = "executed"
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc

        payload = scope.protect(payload)
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"content-type": "application/json", **provider._execution_headers(url)}
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            from .secret_http import open_authenticated
            with (self._response_opener or open_authenticated)(request, timeout=self.timeout) as response:
                from .secrets import redact
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError('Upstream response exceeds 2 MiB')
                raw = redact(json.loads(data.decode("utf-8", "replace")))
        except urllib.error.HTTPError as exc:
            from .secrets import redact
            detail = redact(getattr(exc, '_forge_detail', '') or exc.read(500).decode("utf-8", "replace"))[:300]
            if exc.code == 429:
                # Retry-After from HTTPError.headers (case-insensitive Mapping)
                retry_after = None
                hdrs = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
                if hdrs is not None:
                    ra = hdrs.get("Retry-After")
                    if ra:
                        try:
                            retry_after = float(ra)
                        except (ValueError, TypeError):
                            pass
                raise RateLimited(
                    f"{provider.name}: 429 {detail}",
                    retry_after=retry_after,
                ) from None
            if exc.code in (500, 502, 503, 504, 529):
                raise Overloaded(f"{provider.name}: {exc.code} {detail}") from None
            raise BadRequest(f"{provider.name}: {exc.code} {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"{provider.name}: {exc!r}") from None
        except Exception as exc:
            # Suppress chained transport exceptions containing request headers.
            raise TransportError(f"{provider.name}: {type(exc).__name__}: {exc}") from None

        # usage 归一：各家字段名不一（prompt_cache_hit_tokens /
        # cache_read_input_tokens / prompt_tokens_details.cached_tokens …），
        # 统一映射到 cached / cache_write / reasoning 三类，计费与命中率才有依据。
        usage_raw = raw.get("usage", {}) or {}
        norm = normalize_usage(profile, usage_raw)

        if provider.wire == "anthropic":
            text = "".join(part.get("text", "") for part in raw.get("content", []))
            input_tokens = _safe_count(norm.get("input_tokens", usage_raw.get("input_tokens", 0)))
            # Anthropic 的 input_tokens 不含缓存读与缓存写，总输入量要把两者补回去
            input_tokens += _safe_count(norm.get("cached_tokens", 0)) \
                + _safe_count(norm.get("cache_write_tokens", 0))
            usage = Usage(
                prompt_tokens=input_tokens,
                completion_tokens=_safe_count(norm.get("output_tokens", usage_raw.get("output_tokens", 0))),
                model=model,
                provider=provider.name,
                cached_tokens=_safe_count(norm.get("cached_tokens", 0)),
                cache_write_tokens=_safe_count(norm.get("cache_write_tokens", 0)),
                reasoning_tokens=_safe_count(norm.get("reasoning_tokens", 0)),
            )
            message = {"role": "assistant", "content": raw.get("content", [])}
        else:
            choices = raw.get("choices") or [{}]
            message = choices[0].get("message") or {}
            text = message.get("content") or ""
            usage = Usage(
                prompt_tokens=_safe_count(norm.get("input_tokens", usage_raw.get("prompt_tokens", 0))),
                completion_tokens=_safe_count(norm.get("output_tokens", usage_raw.get("completion_tokens", 0))),
                model=model,
                provider=provider.name,
                cached_tokens=_safe_count(norm.get("cached_tokens", 0)),
                cache_write_tokens=_safe_count(norm.get("cache_write_tokens", 0)),
                reasoning_tokens=_safe_count(norm.get("reasoning_tokens", 0)),
            )

        # 缓存命中后刷新温暖度：下一次同前缀的请求才知道这里还热着。
        flex = plan_raw.get("execution", {}).get("flex")
        if flex:
            tier = raw.get("service_tier")
            flex["status"] = "confirmed" if tier == "flex" else "unconfirmed"
            flex["discount_confirmed"] = tier == "flex"
        _remember_warmth(provider, plan_raw, usage)

        from .toolwire import parse_tool_calls

        # model 必须透传：repair_arguments 的 MODEL_QUIRKS 按模型名开关，
        # 丢掉它等于怪癖表永不生效（旧 _model 属性全树无写入点）。
        calls = parse_tool_calls(message, provider.wire, model=model)
        return text, usage, {
            "tool_calls": [call.to_raw() for call in calls],
            "wire": provider.wire,
            "assistant_message": message,
            "plan": plan_raw,
        }


class ModelRouter:
    """Primary model, retryable-only fallback chain, optional MoA fan-out."""

    def __init__(self, providers: Iterable[Provider], *, transport: Transport | None = None,
                 chain: Iterable[tuple[str, str]] = (), moa: bool = False,
                 moa_panel: Iterable[tuple[str, str]] = (),
                 retries_per_provider: int = 1,
                 primary: tuple[str, str] | None = None) -> None:
        self.providers = {p.name: p for p in providers}
        self.transport = transport or HttpTransport()
        # 2026-09-14 优化：primary 之前被 from_config 读出来又丢掉，导致配置里的主力档
        # （bundles/base.json → model.primary。deepseek）形同虚设。存下来，作为 _order 的默认首选。
        self.primary = tuple(primary) if primary else None
        self.chain = list(chain)
        self.moa = moa
        self.moa_panel = list(moa_panel)
        self.retries_per_provider = retries_per_provider

    # -- vendor adapters -------------------------------------------------
    def profiles(self) -> list[VendorProfile]:
        """每个已注册 provider 的厂商画像（去重前）。"""
        return [p.profile() for p in self.providers.values()]

    def fleet(self) -> dict[str, Any]:
        """当前 fleet 的厂商分布摘要：单厂家还是混排、缓存是否共享。

        用于 `forge vendors` 与 setup 向导：混排模式下每个厂商的缓存是
        互相隔离的，升级到另一个厂商的那一跳必然冷启动、按全价计费。
        """
        from .vendors import describe_fleet

        return describe_fleet(self.profiles())

    @classmethod
    def from_config(cls, cfg, *, transport: Transport | None = None) -> "ModelRouter":
        providers: list[Provider] = []
        for row_id, name, conf in cfg.active():
            if not name.startswith("provider:"):
                continue
            providers.append(Provider(
                name=row_id,
                base_url=str(conf.get("baseURL", conf.get("base_url", ""))),
                api_key=str(conf.get("apiKey", conf.get("api_key", ""))),
                wire=str(conf.get("wire", "openai")),
                models=tuple(conf.get("models") or ()),
                default_model=str(conf.get("model", conf.get("defaultModel", ""))),
                small_model=str(conf.get("smallModel", "")),
                headers=dict(conf.get("headers") or {}),
                rpm=int(conf.get("rpm", 0) or 0),
                service=str(conf.get("service", "")),
                temperature=_as_temperature(conf.get("temperature")),
                vendor=str(conf.get("vendor", "")),
                cache_control=str(conf.get("cacheControl", "auto")),
                expected_calls=int(conf.get("expectedCalls", 2) or 2),
                call_gap_seconds=float(conf.get("callGapSeconds", 60) or 60),
                adapt=bool(conf.get("adapt", True)),
                think_budget=conf.get("thinkBudget"), flex=bool(conf.get("flex", False)),
                defer=bool(conf.get("defer", False)),
                max_defer_seconds=float(conf.get("maxDeferSeconds", 0) or 0),
            ))
        primary = cfg.get("model", "primary", None)
        chain = [tuple(pair) for pair in (cfg.get("model", "fallback", []) or [])]
        panel = [tuple(pair) for pair in (cfg.get("model", "moaModels", []) or [])]
        return cls(
            providers,
            transport=transport,
            chain=chain,
            moa=bool(cfg.get("model", "moa", False)),
            moa_panel=panel,
            primary=tuple(primary) if primary else None,
        )

    # -- routing ---------------------------------------------------------
    def _order(self, primary: tuple[str, str] | None) -> list[tuple[str, str]]:
        ordered: list[tuple[str, str]] = []
        # 调用方没指定时，回落到配置里的主模型（_order 之前的默认首选被丢了）
        primary = primary or self.primary
        if primary:
            ordered.append(tuple(primary) if not isinstance(primary, tuple) else primary)
        ordered.extend(self.chain)
        if not ordered:
            for name, provider in self.providers.items():
                if provider.default_model:
                    ordered.append((name, provider.default_model))
        return ordered

    def complete(self, messages: list[dict[str, Any]], *, primary: tuple[str, str] | None = None,
                 small: bool = False, **options: Any) -> Completion:
        from .secrets import SecretScope
        scope = options.pop('secret_scope', None) or SecretScope()
        messages, options = scope.protect(messages), scope.protect(options)
        attempts: list[dict[str, Any]] = []
        for provider_name, model in self._order(primary):
            provider = self.providers.get(provider_name)
            if provider is None:
                attempts.append({"provider": provider_name, "status": "missing"})
                continue
            if small and provider.small_model:
                model = provider.small_model
            for attempt in range(self.retries_per_provider + 1):
                try:
                    value = self.transport.complete(provider, model, provider._secret_scope.protect(messages),
                                                    **provider._secret_scope.protect(options))
                    if isinstance(value, tuple) and len(value) == 3:
                        text, usage, extra = value
                    else:
                        text, usage = value  # type: ignore[misc]
                        extra = {}
                    attempts.append({"provider": provider_name, "model": model, "status": "ok"})
                    usage.provider = usage.provider or provider_name
                    usage.model = usage.model or model
                    from .secrets import redact
                    text, extra = redact(text), redact(extra)
                    return Completion(text=text, usage=usage, attempts=attempts,
                                      tool_calls=list((extra or {}).get("tool_calls") or []),
                                      wire=str((extra or {}).get("wire") or provider.wire),
                                      assistant_message=(extra or {}).get("assistant_message"),
                                      plan=dict((extra or {}).get("plan") or {}))
                except TransportError as exc:
                    attempts.append({
                        "provider": provider_name,
                        "model": model,
                        "status": "retryable" if exc.retryable else "fatal",
                        "error": str(exc)[:200],
                    })
                    if not exc.retryable:
                        raise
                    if isinstance(exc, RateLimited) and exc.retry_after is not None:
                        wait = max(float(exc.retry_after), 2.0)
                    else:
                        # 2s → 4s → 8s → … capped at 30s; free RPM tiers need
                        # longer backoff than the old 0.5s/4s-cap schedule.
                        wait = min(2.0 * (2 ** attempt), 30.0)
                    if attempt < self.retries_per_provider:
                        time.sleep(wait)  # no backoff when this provider has no next attempt
        raise TransportError(f"all providers failed: {attempts}")

    def complete_moa(self, messages: list[dict[str, Any]], *, models: Iterable[tuple[str, str]],
                     judge: tuple[str, str] | None = None, **options: Any) -> Completion:
        """Fan out to several models, then let a judge synthesise the answer."""
        candidates: list[tuple[str, str]] = []
        attempts: list[dict[str, Any]] = []
        paid: list[tuple[str, Completion]] = []
        for provider_name, model in models:
            try:
                result = self.complete(messages, primary=(provider_name, model), **options)
                candidates.append((f"{provider_name}:{model}", result.text))
                paid.append(("candidate", result))
                attempts.extend(result.attempts)
            except TransportError as exc:
                attempts.append({"provider": provider_name, "status": "failed", "error": str(exc)[:160]})
        if not candidates:
            raise TransportError("MoA produced no candidates")
        if len(candidates) == 1:
            result = paid[0][1]
            result.attempts = attempts
            result.aggregated = True
            result.candidates = [candidates[0][0]]
            return result
        panel = "\n\n".join(f"=== candidate {i+1} ===\n{text}" for i, (_, text) in enumerate(candidates))
        prompt = [
            {"role": "system", "content": "Merge the candidate answers into one best answer. Drop errors."},
            {"role": "user", "content": panel},
        ]
        result = self.complete(prompt, primary=judge, small=judge is None)
        result.attempts = attempts + result.attempts
        result.aggregated = True
        result.candidates = [name for name, _ in candidates]
        return merge_completions(result, paid + [("integrate", result)])


__all__ = [
    "BadRequest",
    "Completion",
    "HttpTransport",
    "ModelRouter",
    "Overloaded",
    "Provider",
    "RateLimited",
    "TransportError",
    "Usage",
]
