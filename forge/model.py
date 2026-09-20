"""Model supply: provider registry + tiered reliability.

OpenCode contributes the provider abstraction (one wire format, providers
declared as data, a separate cheap ``small_model`` for background chores).
Hermes contributes the tiering above it:

    primary  ->  fallback chain (by error class)  ->  optional MoA aggregate

Only *retryable* failures advance the chain. A 400 from a bad request is a bug
in our prompt, not a reason to burn three more providers.
"""

from __future__ import annotations

import json
import time

from .tool_adapter import fill_gemini_name_fields, sanitize_messages
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol


class TransportError(RuntimeError):
    retryable = True


class RateLimited(TransportError):
    retryable = True


class Overloaded(TransportError):
    retryable = True


class BadRequest(TransportError):
    retryable = False


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

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def auth_headers(self) -> dict[str, str]:
        if self.wire == "anthropic":
            return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    provider: str = ""

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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


class Transport(Protocol):
    def complete(self, provider: Provider, model: str, messages: list[dict[str, Any]],
                 **options: Any) -> tuple[str, Usage]: ...


class HttpTransport:
    """Minimal stdlib transport for OpenAI- and Anthropic-shaped endpoints."""

    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout

    def complete(self, provider: Provider, model: str, messages: list[dict[str, Any]],
                 **options: Any) -> tuple[str, Usage]:
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
            url = provider.url("/v1/messages")
        else:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": int(options.get("max_tokens", 2048)),
            }
            if options.get("temperature") is not None:
                payload["temperature"] = options["temperature"]
            if options.get("tools"):
                payload["tools"] = options["tools"]
            url = provider.url("/chat/completions")

        body = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"content-type": "application/json", **provider.auth_headers(), **provider.headers}
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429:
                raise RateLimited(f"{provider.name}: 429 {detail}") from exc
            if exc.code in (500, 502, 503, 504, 529):
                raise Overloaded(f"{provider.name}: {exc.code} {detail}") from exc
            raise BadRequest(f"{provider.name}: {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransportError(f"{provider.name}: {exc!r}") from exc

        if provider.wire == "anthropic":
            text = "".join(part.get("text", "") for part in raw.get("content", []))
            usage_raw = raw.get("usage", {})
            usage = Usage(
                prompt_tokens=int(usage_raw.get("input_tokens", 0)),
                completion_tokens=int(usage_raw.get("output_tokens", 0)),
                model=model,
                provider=provider.name,
            )
            message = {"role": "assistant", "content": raw.get("content", [])}
        else:
            choices = raw.get("choices") or [{}]
            message = choices[0].get("message") or {}
            text = message.get("content") or ""
            usage_raw = raw.get("usage", {})
            usage = Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                completion_tokens=int(usage_raw.get("completion_tokens", 0)),
                model=model,
                provider=provider.name,
            )

        from .toolwire import parse_tool_calls

        calls = parse_tool_calls(message, provider.wire)
        return text, usage, {
            "tool_calls": [call.to_raw() for call in calls],
            "wire": provider.wire,
            "assistant_message": message,
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
                    value = self.transport.complete(provider, model, messages, **options)
                    if isinstance(value, tuple) and len(value) == 3:
                        text, usage, extra = value
                    else:
                        text, usage = value  # type: ignore[misc]
                        extra = {}
                    attempts.append({"provider": provider_name, "model": model, "status": "ok"})
                    return Completion(text=text, usage=usage, attempts=attempts,
                                      tool_calls=list((extra or {}).get("tool_calls") or []),
                                      wire=str((extra or {}).get("wire") or provider.wire),
                                      assistant_message=(extra or {}).get("assistant_message"))
                except TransportError as exc:
                    attempts.append({
                        "provider": provider_name,
                        "model": model,
                        "status": "retryable" if exc.retryable else "fatal",
                        "error": str(exc)[:200],
                    })
                    if not exc.retryable:
                        raise
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
        raise TransportError(f"all providers failed: {attempts}")

    def complete_moa(self, messages: list[dict[str, Any]], *, models: Iterable[tuple[str, str]],
                     judge: tuple[str, str] | None = None, **options: Any) -> Completion:
        """Fan out to several models, then let a judge synthesise the answer."""
        candidates: list[tuple[str, str]] = []
        attempts: list[dict[str, Any]] = []
        for provider_name, model in models:
            try:
                result = self.complete(messages, primary=(provider_name, model), **options)
                candidates.append((f"{provider_name}:{model}", result.text))
                attempts.extend(result.attempts)
            except TransportError as exc:
                attempts.append({"provider": provider_name, "status": "failed", "error": str(exc)[:160]})
        if not candidates:
            raise TransportError("MoA produced no candidates")
        if len(candidates) == 1:
            return Completion(candidates[0][1], Usage(model=candidates[0][0]), attempts, aggregated=True,
                              candidates=[candidates[0][0]])
        panel = "\n\n".join(f"=== candidate {i+1} ===\n{text}" for i, (_, text) in enumerate(candidates))
        prompt = [
            {"role": "system", "content": "Merge the candidate answers into one best answer. Drop errors."},
            {"role": "user", "content": panel},
        ]
        result = self.complete(prompt, primary=judge, small=judge is None)
        return Completion(result.text, result.usage, attempts + result.attempts, aggregated=True,
                          candidates=[name for name, _ in candidates])


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
