"""Smart model routing: three strategies on top of the frozen ModelRouter.

The frozen router (model.py) answers "walk this fixed order". The strategies
answer a different question: "which order should this *task* walk?"

  economy   最便宜可用档优先。按有效单价升序逐档尝试，只在可重试失败时上浮。
            未知价（0.0）取保守解释 = 最贵殿后（见 pricing.EFFECTIVE_RATES
            顶部注释的单一权威定义；loop._pricing_scale 是宽松解释，二者有意不同）。
  balanced  中端主力档首发，失败后向上升级到更高档（现有 chain 兼容）。
  premium   两阶段流水：中端出草稿 → 高端集成裁决。集成段任何失败（含 fatal）
            都降级回草稿——草稿已付费，集成段再贵也没有沉没更多；fatal 只在
            草稿段 raise（那时还没有任何产出）。

Config shape (model row, optional):
    "routing": {
        "strategy": "economy|balanced|premium",
        "tiers": [["lite", "model-name"], ...],            # cheap→expensive
        "premium": [["premium", "model-name"], ...],      # 集成裁决档
        "small":  ["lite", "model-name"]                  # 杂务档
    }

Nothing here overrides the frozen contract: SmartRouter subclasses
ModelRouter, reuses its retry/fallback semantics per hop, and stays a drop-in
replacement for Agent/loop (same .complete() signature).

R1-2 (rev.R2-0.7.0): SmartRouter overrides _order(None) so _ModelProbe reads
the STRATEGY-first tier (economy = cheapest priced tier), not the frozen
primary — the compaction budget now scales off the tier actually drafted
first. With tiers still empty (no routing block), _order falls back to the
frozen primary + chain, unchanged.

premium integration stage (R3-2/R4-2): the finisher receives ONLY the draft
plus the original task — not the full conversation. The refine turn is
re-shaped to system + user(task) + assistant(draft) + user(refine-ask), which
preserves strict role alternation for anthropic-wire finishers (no
consecutive-user 400 risk) and keeps the data plane small (完整对话不给
集成档）。若 premium 配置了集成档，即视为知情同意把草稿与原任务发给该档。

Small-chore isolation (R3-3): ``small=True`` walks ONLY the small tier —
no chain, no premium. A summary/title chore must never burn the review tier.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import (
    Completion,
    ModelRouter,
    Provider,
    TransportError,
    Usage,
)
from .pricing import rate_for

STRATEGIES = ("economy", "balanced", "premium")

# R2-2: single-element tier pairs are rejected at parse time (later writes
# win per DSH semantics, so a bad literal must fail loudly, not silently).
class RoutingConfigError(ValueError):
    """Raised when the model.routing block has a malformed shape."""


def _pair(value: Any, where: str) -> tuple[str, str] | None:
    """Strict (provider, model) pair parser: strings only, length exactly 2."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2 \
            and all(isinstance(x, str) for x in value):
        return (value[0], value[1])
    raise RoutingConfigError(
        f"routing.{where}: expected [provider, model] pair of two strings, got {value!r}")


@dataclass
class RoutingConfig:
    """Validated view of the ``model.routing`` config block (R2-2)."""

    strategy: str = "balanced"
    tiers: list[tuple[str, str]] = field(default_factory=list)
    premium: list[tuple[str, str]] = field(default_factory=list)
    small: tuple[str, str] | None = None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            self.warnings.append(f"unknown strategy {self.strategy!r} -> balanced")
            self.strategy = "balanced"
        # R2-2: strict pair validation. A bad literal raises with the row
        # path in the message; malformed small degrades to None (chore calls
        # then fall through to the normal path instead of per-character
        # provider names).
        self.tiers = [p for p in (_pair(v, f"tiers[{i}]") for i, v in enumerate(self.tiers or []))
                      if p is not None]
        self.premium = [p for p in (_pair(v, f"premium[{i}]") for i, v in enumerate(self.premium or []))
                        if p is not None]
        if self.small is not None:
            try:
                self.small = _pair(self.small, "small")
            except RoutingConfigError as exc:
                self.warnings.append(str(exc))
                self.small = None

    @classmethod
    def from_value(cls, value: Any) -> "RoutingConfig":
        """Parse the routing block: dict, bare strategy name, or None.

        Raises RoutingConfigError with the offending value for malformed
        tiers/premium; ``small`` is non-critical and degrades to None with a
        warning instead.
        """
        if value is None:
            return cls()
        if isinstance(value, str):
            return cls(strategy=value)
        if isinstance(value, dict):
            return cls(
                strategy=str(value.get("strategy", "balanced")),
                tiers=list(value.get("tiers") or []),
                premium=list(value.get("premium") or []),
                small=tuple(value["small"]) if value.get("small") else None,
            )
        return cls()

    def to_raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {"strategy": self.strategy}
        if self.tiers:
            out["tiers"] = [list(pair) for pair in self.tiers]
        if self.premium:
            out["premium"] = [list(pair) for pair in self.premium]
        if self.small:
            out["small"] = list(self.small)
        return out


def _tier_rate(pair: tuple[str, str]) -> float:
    """Effective blended rate of a (provider, model) tier; unknown = 0."""
    return rate_for(str(pair[1]))


def _sorted_by_cost(tiers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Cheap→expensive; unknown-rate tiers last (keep capacity, price honestly).

    With the seeded invoice table: mimo (¥0.0754) < deepseek (¥0.1595) <
    review-tier (0.0 = unknown → treated as most expensive, not free).
    未知价的解释回指 pricing.EFFECTIVE_RATES 顶部的单一权威定义。
    """
    known = [p for p in tiers if _tier_rate(p) > 0]
    unknown = [p for p in tiers if _tier_rate(p) <= 0]
    return sorted(known, key=_tier_rate) + unknown


class SmartRouter(ModelRouter):
    """Three-task-strategy router. Drop-in ModelRouter replacement.

    economy:   walk tiers cheapest-first (pricing.py rates), retryable
               failures only advance to the next tier.
    balanced:  first tier first (中端主力), then climb tiers on retryable
               failure — a generalisation of the frozen fixed chain.
    premium:   two-stage pipeline. Stage 1 drafts on the FIRST (mid) tier;
               stage 2 lets the PREMIUM tier integrate/adjudicate the draft
               (frozen router's MoA, narrowed to a draft→judge pair). ANY
               integration failure — retryable OR fatal — degrades to the
               draft (R3-1: the draft is already paid for; a fatal from the
               finisher is about THAT request, not the draft). Fatal only
               raises in the DRAFT stage, where nothing has been produced.

    Unknown strategies fall back to balanced (logged in attempts).
    """

    def __init__(self, providers: Iterable[Provider], *, transport: Any = None,
                 routing: RoutingConfig | None = None,
                 chain: Iterable[tuple[str, str]] = (),
                 moa: bool = False,
                 moa_panel: Iterable[tuple[str, str]] = (),
                 retries_per_provider: int = 1,
                 primary: tuple[str, str] | None = None) -> None:
        super().__init__(providers, transport=transport, chain=chain, moa=moa,
                         moa_panel=moa_panel, retries_per_provider=retries_per_provider,
                         primary=primary)
        self.routing = routing or RoutingConfig()

    @classmethod
    def from_config(cls, cfg, *, transport: Any = None) -> "SmartRouter":
        router = ModelRouter.from_config(cfg, transport=transport)
        routing = RoutingConfig.from_value(cfg.get("model", "routing", None))
        return cls(router.providers.values(), transport=router.transport,
                   routing=routing, chain=router.chain, moa=router.moa,
                   moa_panel=router.moa_panel, retries_per_provider=router.retries_per_provider,
                   primary=router.primary)

    # -- strategy orders --------------------------------------------------
    def _economy_order(self) -> list[tuple[str, str]]:
        return _sorted_by_cost(self.routing.tiers)

    def _balanced_order(self) -> list[tuple[str, str]]:
        # 中端首发 = tiers 的第一档；失败向上爬 tiers，最后接冻结 chain。
        return list(self.routing.tiers) + list(self.chain)

    def _premium_pair(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        drafters = list(self.routing.tiers)          # 中端草稿档（第 1 档首发）
        finishers = list(self.routing.premium)       # 高端集成档
        return drafters, finishers

    # R1-2: _ModelProbe reads _order(None)[0]; with a routing block, that
    # must be the strategy-first tier, not the frozen primary.
    def _order(self, primary: tuple[str, str] | None) -> list[tuple[str, str]]:
        if primary is None and self.routing.tiers:
            if self.routing.strategy == "economy":
                return self._economy_order()
            return list(self.routing.tiers)
        return super()._order(primary)

    # -- public surface ----------------------------------------------------
    def complete(self, messages: list[dict[str, Any]], *, primary: tuple[str, str] | None = None,
                 small: bool = False, **options: Any) -> Completion:
        strategy = self.routing.strategy
        # 显式 primary 优先（调用方点名 = 直接走冻结语义）。
        if primary is not None:
            return super().complete(messages, primary=primary, small=small, **options)
        # R3-3: small chores walk ONLY the small tier — no chain, no climb.
        # A summary/title call must never burn the review tier.
        if small and self.routing.small:
            provider = self.providers.get(self.routing.small[0])
            if provider is None:
                raise TransportError(f"small tier provider missing: {self.routing.small!r}")
            value = self.transport.complete(provider, self.routing.small[1], messages, **options)
            text, usage, extra = self._split(value)
            return self._completion(text, usage, [
                {"provider": self.routing.small[0], "model": self.routing.small[1],
                 "status": "ok", "strategy": "small"}], extra, provider)
        # small requested but no small tier configured → fall through to the
        # normal strategy (honest: chores ride the main tier, visible in
        # attempts via the strategy label).

        if strategy == "economy":
            return self._complete_economy(messages, **options)
        if strategy == "premium":
            return self._complete_premium(messages, **options)
        return self._complete_balanced(messages, **options)

    # -- shared walk (R2-3: one retry/walk implementation, not three) -------
    def _walk(self, order: list[tuple[str, str]], messages: list[dict[str, Any]],
              label: str, stage: str | None = None, **options: Any) -> tuple[Completion, list[dict[str, Any]]]:
        """Walk an ordered tier list with frozen retry semantics.

        Raises TransportError on fatal (propagates) or when every tier failed
        retryably. Returns (completion, attempts) — callers may keep walking
        with the same attempts list (premium degrade path reuses it).
        """
        attempts: list[dict[str, Any]] = []
        for provider_name, model in order:
            provider = self.providers.get(provider_name)
            if provider is None:
                event: dict[str, Any] = {"provider": provider_name, "status": "missing", "strategy": label}
                if stage:
                    event["stage"] = stage
                attempts.append(event)
                continue
            for attempt in range(self.retries_per_provider + 1):
                try:
                    value = self.transport.complete(provider, model, messages, **options)
                    text, usage, extra = self._split(value)
                    ok_event: dict[str, Any] = {"provider": provider_name, "model": model,
                                                "status": "ok", "strategy": label}
                    if stage:
                        ok_event["stage"] = stage
                    attempts.append(ok_event)
                    # R2-4: fill provider/model ourselves — never depend on
                    # the transport's good manners for candidate bookkeeping.
                    usage.provider = usage.provider or provider_name
                    usage.model = usage.model or model
                    return self._completion(text, usage, attempts, extra, provider), attempts
                except TransportError as exc:
                    fail_event: dict[str, Any] = {"provider": provider_name, "model": model,
                                                  "status": "retryable" if exc.retryable else "fatal",
                                                  "strategy": label, "error": str(exc)[:200]}
                    if stage:
                        fail_event["stage"] = stage
                    attempts.append(fail_event)
                    if not exc.retryable:
                        raise
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
        raise TransportError(f"{label}: all tiers failed: {attempts}")

    # -- strategies ---------------------------------------------------------
    def _complete_economy(self, messages: list[dict[str, Any]], **options: Any) -> Completion:
        order = self._economy_order()
        if not order:
            return super().complete(messages, small=False, **options)
        completion, _ = self._walk(order, messages, "economy", **options)
        return completion

    def _complete_balanced(self, messages: list[dict[str, Any]], **options: Any) -> Completion:
        order = self._balanced_order()
        if not order:
            return super().complete(messages, small=False, **options)
        completion, _ = self._walk(order, messages, "balanced", **options)
        return completion

    def _complete_premium(self, messages: list[dict[str, Any]], **options: Any) -> Completion:
        drafters, finishers = self._premium_pair()
        if not drafters or not finishers:
            # 配置不全 = 退回 balanced 语义（诚实降级，不假装走了两阶段）。
            attempts = [{"strategy": "premium", "status": "degraded",
                         "reason": "missing drafters or finishers"}]
            completion = self._complete_balanced(messages, **options)
            completion.attempts = attempts + completion.attempts
            return completion

        # Stage 1: draft on the FIRST mid tier (tier climbing via _walk).
        # Fatal here raises — nothing has been produced yet (主张 4 的原始语义)。
        draft, attempts = self._walk(drafters, messages, "premium", stage="draft", **options)

        # 工具调用步守卫：草稿要求执行工具 = 主循环中段，集成无意义（会把
        # 工具结果和草稿文本搅在一起）。直接回流，事件里如实记 skipped。
        if draft.tool_calls:
            draft.attempts.append({"strategy": "premium", "stage": "integrate",
                                   "status": "skipped", "reason": "draft is a tool-call step"})
            return draft

        # Stage 2: premium integration/adjudication.
        # R3-2: strict role alternation — the draft is carried as an
        # ASSISTANT turn, the refine ask as a USER turn. No consecutive-user
        # pair survives this shaping (anthropic wire 400 risk eliminated).
        # R4-2: the finisher gets ONLY the original task + the draft — the
        # full conversation (tool results included) does NOT leak to the
        # premium provider; data plane shrinks instead of grows.
        task_text = ""
        for m in messages:
            if m.get("role") == "user":
                task_text = str(m.get("content") or "")
        refine_messages = [
            {"role": "system", "content": "You are the integration stage of a two-stage pipeline. Refine the assistant draft into the final answer for the user task."},
            {"role": "user", "content": task_text or "(see draft)"},
            {"role": "assistant", "content": draft.text},
            {"role": "user", "content": "Refine, correct and complete this draft. Output the final answer only."},
        ]
        refine_options = {k: v for k, v in options.items() if k != "tools"}
        for provider_name, model in finishers:
            provider = self.providers.get(provider_name)
            if provider is None:
                attempts.append({"provider": provider_name, "status": "missing", "strategy": "premium",
                                 "stage": "integrate"})
                continue
            for attempt in range(self.retries_per_provider + 1):
                try:
                    value = self.transport.complete(provider, model, refine_messages, **refine_options)
                    text, usage, extra = self._split(value)
                    ok_event: dict[str, Any] = {"provider": provider_name, "model": model, "status": "ok",
                                                "strategy": "premium", "stage": "integrate"}
                    attempts.append(ok_event)
                    merged = self._completion(text, usage, attempts, extra, provider)
                    usage.provider = usage.provider or provider_name
                    usage.model = usage.model or model
                    # 计费诚实：_finish 从 report.usage 记账，premium 一通调用
                    # 烧了两个阶段，usage 必须是两阶段之和，否则账本漏记草稿段。
                    merged.usage = Usage(
                        prompt_tokens=draft.usage.prompt_tokens + usage.prompt_tokens,
                        completion_tokens=draft.usage.completion_tokens + usage.completion_tokens,
                        model=model, provider=provider_name)
                    merged.aggregated = True
                    merged.candidates = [f"{draft.usage.provider}:{draft.usage.model}", f"{provider_name}:{model}"]
                    return merged
                except TransportError as exc:
                    attempts.append({"provider": provider_name, "model": model,
                                     "status": "retryable" if exc.retryable else "fatal",
                                     "strategy": "premium", "stage": "integrate",
                                     "error": str(exc)[:200]})
                    if not exc.retryable:
                        # R3-1: integration fatal ALSO degrades to the draft.
                        # The fatal verdict is about the finisher's view of
                        # THIS request, not about the draft's worth; the
                        # draft is already paid for and returned honestly
                        # (aggregated=False, degraded event recorded).
                        break
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
        # 所有 finisher 失败（可重试穷尽或 fatal）→ 优雅降级：草稿就是答案。
        # draft 持有的是快照；这里取 attempts 的完整活链（草稿+集成事件，各一次）。
        draft.attempts = list(attempts)
        draft.attempts.append({"strategy": "premium", "stage": "integrate", "status": "degraded",
                               "reason": "all finishers failed; draft returned"})
        draft.aggregated = False
        draft.candidates = [f"{draft.usage.provider}:{draft.usage.model}"]
        return draft

    # -- shared helpers ------------------------------------------------------
    @staticmethod
    def _split(value: Any) -> tuple[str, Usage, dict[str, Any]]:
        if isinstance(value, tuple) and len(value) == 3:
            return value[0], value[1], value[2] or {}
        text, usage = value  # type: ignore[misc]
        return text, usage, {}

    def _completion(self, text: str, usage: Usage, attempts: list[dict[str, Any]],
                    extra: dict[str, Any], provider: Provider) -> Completion:
        return Completion(
            text=text, usage=usage, attempts=attempts,
            tool_calls=list(extra.get("tool_calls") or []),
            wire=str(extra.get("wire") or provider.wire),
            assistant_message=extra.get("assistant_message"),
        )


__all__ = ["RoutingConfig", "RoutingConfigError", "SmartRouter", "STRATEGIES"]
