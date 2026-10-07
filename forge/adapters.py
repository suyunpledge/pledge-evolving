"""Vendor adapters — each vendor decides how *this* request should be sent.

Difference from ``vendors.py``
------------------------------
``vendors.py`` is a table: it says *what a vendor's billing is*. This module is
behaviour: it says *what to do about it for this request*. A table can answer
"Anthropic's cache read is 0.10x"; only an adapter can answer "given that this
conversation will make 6 more calls within 5 minutes, cache the system prompt
at 5-minute TTL with two breakpoints, and here is the ¥ you save".

Every adapter implements the same three-step shape:

1. **enumerate** the legally-available plans for this vendor
   (cache / don't cache, 5m / 1h TTL, one / two breakpoints, batch / standard …)
2. **price** each plan against the expected call pattern
3. **pick** the cheapest one and return it as a ``RequestPlan``

The result is a plan, not a boolean. The plan carries the payload mutations, the
cost estimate, what it saved against the naive send, and the human-readable
actions — so the run report can explain the invoice instead of just printing it.

Cost model
----------
Only the **cacheable prefix** (tools + system) gets cache treatment. The tail
of the conversation is charged at the full input rate on every call: it is
different every turn, so it is never read from cache. Pricing the two together
(as a naive implementation does) understates the cost of long user messages and
overstates what caching can save.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .context_plan import estimate_text, estimate_tokens
from .vendors import (
    PROFILES,
    VendorProfile,
    price_multiplier,
)

MILLION = 1_000_000.0


# --------------------------------------------------------------------------- #
# plan context
# --------------------------------------------------------------------------- #


@dataclass
class PlanContext:
    """Everything an adapter needs to price a decision.

    ``calls_expected`` is the number of *future* calls that will reuse the same
    prefix. It is the single most important input: a cache write only pays for
    itself if something later reads it. Callers that do not know should pass 2
    (the conservative "one more call" assumption), never 1.
    """

    profile: VendorProfile
    model: str = ""
    base_input: float = 0.0        # CNY / 1M tokens, uncached input
    base_output: float = 0.0       # CNY / 1M tokens
    calls_expected: int = 2
    gap_seconds: float = 60.0      # typical spacing between those calls
    interactive: bool = False
    think_budget: int | None = None
    batch: bool = False
    flex: bool = False
    warm: bool = False             # this exact prefix is already cached here
    when: float | None = None
    holiday: bool = False
    # Wire actually used for this request. Empty means "assume compatible"
    # (direct adapter calls in tests). ``explicit_anthropic`` markers may only
    # ride an anthropic wire; on an openai-wire gateway they would be junk.
    wire: str = ""
    # Cache marker policy: auto (adapter decides) / off (never) / explicit (force).
    cache_control: str = "auto"


@dataclass
class RequestPlan:
    """The decision, with enough detail to be audited after the fact."""

    vendor: str
    vendor_label: str
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # payload mutations the caller must apply
    cache_control: bool = False
    breakpoints: int = 0
    ttl: str = ""

    # economics — both figures are the cost of the SAME expected call window,
    # so their difference is a saving rather than an artefact of two horizons.
    est_tokens: int = 0            # full prompt tokens (prefix + tail)
    est_cost: float = 0.0          # CNY over the reuse window, under this plan
    baseline_cost: float = 0.0     # CNY over the same window, sent naively
    saving: float = 0.0
    saving_pct: float = 0.0
    penalty: float = 0.0           # CNY the plan *accepts* (long-context cliff)

    # shape hints
    cliff_action: str = "pass"     # pass | compact | split
    reasoning_budget: int | None = None
    defer: bool = False            # cheaper if we wait for the off-peak window
    cache_engages: bool = True     # False when the prefix cannot be cached here

    def to_raw(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor, "vendor_label": self.vendor_label,
            "actions": list(self.actions), "notes": list(self.notes),
            "cache_control": self.cache_control, "breakpoints": self.breakpoints,
            "ttl": self.ttl, "est_tokens": self.est_tokens,
            "est_cost": round(self.est_cost, 6),
            "baseline_cost": round(self.baseline_cost, 6),
            "saving": round(self.saving, 6), "saving_pct": round(self.saving_pct, 1),
            "cliff_action": self.cliff_action,
            "reasoning_budget": self.reasoning_budget,
            "defer": self.defer, "cache_engages": self.cache_engages,
        }


# --------------------------------------------------------------------------- #
# cost primitives
# --------------------------------------------------------------------------- #


def _cache_series(prefix: int, tail: int, calls: int, *, write_mult: float,
                  read_mult: float, price: float, modifier: float = 1.0,
                  warm: bool = False) -> float:
    """CNY to send the same prompt ``calls`` times with the prefix cached.

    Cold: one write, then ``calls - 1`` reads.
    Warm: no write, and **all** ``calls`` read — the current call is a read too,
    which is the whole point of a warm prefix.
    The tail is uncacheable and pays full price every time.
    """
    p = price * modifier / MILLION
    reads = calls if warm else max(0, calls - 1)
    return prefix * p * (write_mult + reads * read_mult) + tail * p * calls


def _uncached_series(prefix: int, tail: int, calls: int, *, price: float,
                     modifier: float = 1.0) -> float:
    return (prefix + tail) * calls * price * modifier / MILLION


def breakeven_calls(profile: VendorProfile) -> int:
    """How many calls it takes for caching to beat not caching (plain maths).

    Solves ``w + (N-1)h < N`` for the smallest integer ``N``. Reported to the
    user so "turn caching on" is a number, not a slogan.
    """
    w, h = profile.cache_write_multiplier, profile.cache_read_multiplier
    if profile.cache_mode in ("none", "unknown"):
        return 10 ** 9
    if h >= 1.0:
        return 10 ** 9
    if w <= h:
        return 2                      # writing at or below the read price ties on call 1
    raw = (w - h) / (1.0 - h)
    return max(2, int(raw) + 1)


def ttl_choice(profile: VendorProfile, gap_seconds: float) -> str:
    """Pick the TTL whose break-even the call pattern actually clears.

    A longer TTL costs more to write, so it only wins when the calls are too
    far apart for the short TTL to survive.
    """
    if profile.cache_ttl in ("disk", "managed", "unknown", "none"):
        return profile.cache_ttl
    if gap_seconds <= 270:
        return profile.cache_ttl
    if profile.cache_ttl == "5m":
        # Compare one 2x write + cheap reads against re-writing at 1.25x each call.
        calls = max(2, int(3600 / max(gap_seconds, 1)))
        long_ttl = 2.0 + 0.1 * (calls - 1)
        rewrite = 1.25 * calls
        if long_ttl < rewrite:
            return "1h"
        return "1h"
    return profile.cache_ttl


def _write_multiplier(profile: VendorProfile, ttl: str) -> float:
    """Write premium for the chosen TTL (1h costs 2x on Anthropic)."""
    if ttl == "1h" and profile.cache_ttl == "5m":
        return 2.0
    return profile.cache_write_multiplier


# --------------------------------------------------------------------------- #
# adapter protocol
# --------------------------------------------------------------------------- #


class VendorAdapter:
    """Base adapter: prices the generic cache question, applies the vendor hook.

    Subclasses override ``extra_actions`` for the vendor-specific parts that the
    generic maths cannot see (Bailian's mutually-exclusive modes, Gemini's 4096
    floor, DeepSeek's peak window …).
    """

    vendor_id = "generic"

    def __init__(self, profile: VendorProfile) -> None:
        self.profile = profile

    # -- overridable ------------------------------------------------------
    def prefix_text(self, payload: dict[str, Any],
                    messages: list[dict[str, Any]]) -> str:
        """The cacheable prefix as text: top-level system + system-role
        messages + tool declarations.

        System-role *messages* count here too: agent frameworks frequently put
        the durable instructions in a ``role: system`` message rather than the
        top-level ``system`` field, and ignoring them makes the prefix look
        empty on exactly the vendors with the highest cache floors.
        """
        parts: list[str] = []
        system = payload.get("system")
        if system:
            parts.append(system if isinstance(system, str)
                         else json.dumps(system, ensure_ascii=False, default=str))
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                content = message.get("content")
                parts.append(content if isinstance(content, str)
                             else json.dumps(content, ensure_ascii=False, default=str))
        for tool in (payload.get("tools") or []):
            parts.append(tool if isinstance(tool, str)
                         else json.dumps(tool, ensure_ascii=False, default=str))
        return "".join(parts)

    def prefix_tokens(self, payload: dict[str, Any],
                      messages: list[dict[str, Any]]) -> int:
        return estimate_text(self.prefix_text(payload, messages))

    def tail_tokens(self, payload: dict[str, Any],
                    messages: list[dict[str, Any]]) -> int:
        """Uncacheable conversation tail (everything that is not system)."""
        tail = [m for m in messages
                if not (isinstance(m, dict) and m.get("role") == "system")]
        return estimate_tokens(tail)

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan,
                      prefix_tokens: int) -> None:
        """Vendor-specific additions to the plan."""

    # -- the pipeline -----------------------------------------------------
    def plan(self, payload: dict[str, Any], messages: list[dict[str, Any]],
             ctx: PlanContext) -> RequestPlan:
        profile = self.profile
        prefix = self.prefix_tokens(payload, messages)
        tail = self.tail_tokens(payload, messages)
        est = prefix + tail
        price = ctx.base_input or 0.0

        plan = RequestPlan(vendor=profile.id, vendor_label=profile.label,
                           est_tokens=est)
        modifier = price_multiplier(profile, when=ctx.when, holiday=ctx.holiday,
                                    batch=ctx.batch, flex=ctx.flex,
                                    prompt_tokens=est)
        plan.baseline_cost = _uncached_series(prefix, tail, max(1, ctx.calls_expected),
                                              price=price, modifier=modifier)

        mode = (ctx.cache_control or "auto").strip().lower()
        force = mode in ("explicit", "on", "true", "1")
        disabled = mode in ("off", "none", "false", "0")

        # -- cliff ---------------------------------------------------------
        if profile.long_context_threshold and est > profile.long_context_threshold:
            plan.cliff_action = "compact"
            plan.penalty = plan.baseline_cost * (profile.long_context_multiplier - 1.0)
            plan.actions.append(
                f"越过长上下文阶梯（{est:,} > {profile.long_context_threshold:,}），"
                f"单价 ×{profile.long_context_multiplier:g}")

        # -- hard off ------------------------------------------------------
        if disabled:
            plan.cache_engages = False
            plan.actions.append("缓存已被显式关闭（cacheControl=off）")
            self.extra_actions(ctx, plan, prefix)
            return _finalise(plan)

        # -- wire compatibility ---------------------------------------------
        if (profile.cache_mode == "explicit_anthropic" and ctx.wire
                and ctx.wire != "anthropic"):
            plan.cache_engages = False
            plan.actions.append(
                f"该端点走 {ctx.wire} 协议，Anthropic 的 cache_control 不能上线，本次不缓存")
            self.extra_actions(ctx, plan, prefix)
            return _finalise(plan)

        # -- can this prefix be cached here at all? -------------------------
        if profile.cache_mode == "none" or price <= 0:
            plan.cache_engages = False
            plan.notes.append("该厂商不按 token 计费，缓存决策不适用")
            self.extra_actions(ctx, plan, prefix)
            return _finalise(plan)

        if profile.cache_mode == "unknown":
            # 口径未知：既没有标记可打，也不能假装命中。
            plan.cache_engages = False
            plan.actions.append("该厂商缓存计费口径未知，本框架不假设命中，按原价估算")
            self.extra_actions(ctx, plan, prefix)
            return _finalise(plan)

        if prefix < profile.min_cache_tokens and not force:
            plan.cache_engages = False
            plan.actions.append(
                f"稳定前缀 {prefix:,} token 低于该厂商最小可缓存 {profile.min_cache_tokens:,}，"
                f"本次不缓存（补齐稳定内容反而更划算）")
            self.extra_actions(ctx, plan, prefix)
            return _finalise(plan)

        # -- enumerate cache candidates and pick the cheapest --------------
        calls = max(1, ctx.calls_expected)
        ttl = ttl_choice(profile, ctx.gap_seconds)
        write_mult = _write_multiplier(profile, ttl)
        bps = self.breakpoint_count(payload)

        if ctx.warm:
            cached = _cache_series(prefix, tail, calls, write_mult=0.0,
                                   read_mult=profile.cache_read_multiplier,
                                   price=price, modifier=modifier, warm=True)
        else:
            cached = _cache_series(prefix, tail, calls, write_mult=write_mult,
                                   read_mult=profile.cache_read_multiplier,
                                   price=price, modifier=modifier)

        candidates: list[tuple[str, float, int, str]] = [
            (f"cache-{ttl}", cached, bps, ttl)]
        if not force:
            candidates.append(("no-cache", plan.baseline_cost, 0, ""))
            if ttl == "5m" and profile.cache_ttl == "5m" and ctx.gap_seconds > 270:
                long_cost = _cache_series(prefix, tail, calls, write_mult=2.0,
                                          read_mult=profile.cache_read_multiplier,
                                          price=price, modifier=modifier)
                candidates.append(("cache-1h", long_cost, bps, "1h"))

        label, best_cost, best_bps, best_ttl = min(candidates, key=lambda c: c[1])
        best_write = _write_multiplier(profile, best_ttl)

        if label == "no-cache":
            plan.cache_control = False
            plan.est_cost = best_cost
            plan.actions.append(
                f"本次不缓存更省（预期复用 {calls} 次，"
                f"未达回本点 {breakeven_calls(profile)} 次）")
        else:
            plan.cache_control = self.should_mark(ctx)
            plan.breakpoints = best_bps
            plan.ttl = best_ttl
            plan.est_cost = best_cost
            if plan.cache_control:
                plan.actions.append(
                    f"注入 cache_control ×{best_bps}（TTL {best_ttl}，"
                    f"写入 {best_write:g}× / 命中 {profile.cache_read_multiplier:g}×）")
            else:
                plan.actions.append(
                    f"依赖厂商自动缓存（命中 {profile.cache_read_multiplier:g}×），无需标记")

        plan.saving = max(0.0, plan.baseline_cost - plan.est_cost)
        if plan.baseline_cost > 0:
            plan.saving_pct = round(plan.saving / plan.baseline_cost * 100, 1)

        self.extra_actions(ctx, plan, prefix)
        return _finalise(plan)

    # -- helpers ----------------------------------------------------------
    def breakpoint_count(self, payload: dict[str, Any]) -> int:
        """One breakpoint per cacheable layer: tools, then system."""
        n = 0
        if payload.get("tools"):
            n += 1
        if payload.get("system"):
            n += 1
        return min(n, 4) or 1

    def should_mark(self, ctx: PlanContext) -> bool:
        """Whether this vendor needs an explicit marker on the wire."""
        return self.profile.explicit_cache


def _finalise(plan: RequestPlan) -> RequestPlan:
    if plan.est_cost <= 0:
        plan.est_cost = plan.baseline_cost
    return plan


# --------------------------------------------------------------------------- #
# concrete adapters
# --------------------------------------------------------------------------- #


class DeepSeekAdapter(VendorAdapter):
    """Cache is free and reads cost 2%. The only real levers are *when* and
    how much thinking is spent — both of which are outside the payload."""

    vendor_id = "deepseek"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        if ctx.profile.peak_valley:
            from .vendors import is_peak

            if is_peak(ctx.profile, ctx.when, holiday=ctx.holiday) and not ctx.interactive:
                plan.defer = True
                plan.actions.append("当前处于高峰时段，同样的请求押到低谷可省 50%")
        if ctx.think_budget is None:
            plan.reasoning_budget = 4096
            plan.notes.append("思考 token 按输出价计费；4096 仅为预算建议，以 execution 的执行状态为准")


class AnthropicAdapter(VendorAdapter):
    """The only vendor where nothing is cached unless we say so, so the
    breakpoint placement *is* the product."""

    vendor_id = "anthropic"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        plan.notes.append(
            f"回本点 {breakeven_calls(ctx.profile)} 次调用；"
            f"1 小时档写入 2×，仅在调用间隔 >4.5 分钟时才划算")
        if ctx.interactive and ctx.calls_expected >= 3 and prefix_tokens >= 4096:
            plan.notes.append("可用 max_tokens=0 预暖系统提示，削掉首请求的冷启动延迟")


class OpenAIAdapter(VendorAdapter):
    """Automatic caching, so the adapter's job is to protect the *boundary*:
    never rewrite history, and keep tool definitions byte-stable."""

    vendor_id = "openai"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        if ctx.flex and ctx.profile.supports_flex:
            plan.actions.append("走 Flex 经济档（约 5 折，牺牲延迟）")
        if ctx.batch and ctx.profile.batch_discount < 1.0:
            plan.actions.append(f"走 Batch（{ctx.profile.batch_discount:g} 折，异步）")
        plan.notes.append("隐式断点落在最新一条合格消息结尾；改写历史会让断点失配")


class GeminiAdapter(VendorAdapter):
    """A 4096-token floor is high enough that most system prompts miss it —
    so 'why is my cache not engaging' has a numeric answer here."""

    vendor_id = "gemini"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        floor = ctx.profile.min_cache_tokens
        if prefix_tokens < floor:
            plan.cache_engages = False
            plan.actions.append(
                f"稳定前缀 {prefix_tokens:,} < 门槛 {floor:,}：把参考资料补进前缀即可让缓存生效，"
                f"这是免费的杠杆")
        else:
            plan.actions.append(f"前缀 {prefix_tokens:,} 已越过 {floor:,} 门槛，隐式缓存可生效")
        if ctx.profile.long_context_threshold:
            plan.notes.append("超 200K 输入输出同时翻倍；显式缓存另收存储费")


class BailianAdapter(VendorAdapter):
    """Explicit and implicit caching are mutually exclusive, so this adapter
    is a genuine either/or decision rather than a yes/no one."""

    vendor_id = "bailian"

    def should_mark(self, ctx: PlanContext) -> bool:
        mode = (ctx.cache_control or "auto").strip().lower()
        if mode in ("explicit", "on", "true", "1"):
            return True
        return self.choose_mode(ctx) == "explicit"

    def choose_mode(self, ctx: PlanContext, *, prefix_tokens: int = 0) -> str:
        """explicit wins at >=4 calls in the 5-minute window; implicit below.

        1.25 + 0.10(N-1)  <  1.00 + 0.20(N-1)  →  N >= 4
        """
        return "explicit" if ctx.calls_expected >= 4 else "implicit"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        if not plan.cache_engages:
            return
        mode = ("explicit" if ctx.cache_control in ("explicit", "on", "true", "1")
                else self.choose_mode(ctx))
        if mode == "explicit":
            plan.actions.append(
                f"选显式缓存（预期复用 {ctx.calls_expected} 次，"
                f"写入 1.25× / 命中 0.10× 优于隐式 0.20×）")
        else:
            plan.cache_control = False
            plan.actions.append(
                f"选隐式缓存（预期复用 {ctx.calls_expected} 次 < 4，"
                f"免写入费，命中 0.20× 更划算）")
        plan.notes.append("回溯窗口 20 个 content 块；并行工具结果建议合并为一条消息")


class ZhipuAdapter(VendorAdapter):
    """Cheapest automatic cache among the domestic vendors, and the thinking
    tiers are the real cost dial."""

    vendor_id = "zhipu"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        if ctx.think_budget is None:
            plan.reasoning_budget = 4096
            plan.notes.append("思考 token 按输出价计费；low/high/max 三档里 max 最贵")
        plan.notes.append(f"最小前缀仅 {ctx.profile.min_cache_tokens}，短系统提示也能命中")


class KimiAdapter(VendorAdapter):
    """k3 bills cache writes separately, so the write is a real cost here."""

    vendor_id = "kimi"

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        plan.notes.append("缓存写入单独计费（k3）；命中后 TTL 自动续期，不再收写入费")


class ConservativeAdapter(VendorAdapter):
    """Unknown vendors: no marker, no invented discount, and say so."""

    vendor_id = "generic"

    def should_mark(self, ctx: PlanContext) -> bool:
        return False

    def extra_actions(self, ctx: PlanContext, plan: RequestPlan, prefix_tokens: int) -> None:
        if plan.cache_engages and ctx.profile.id == "generic":
            plan.actions.append("未识别的端点：不注入任何厂商专属字段，按原价估算")
        if ctx.profile.id == "generic":
            plan.notes.append("若已知该端点的缓存口径，可在 provider 行填 vendor 字段显式指定")
        else:
            plan.notes.append("按已识别的厂商画像规划；未来命中仍需以上游 usage 验证")


class LocalAdapter(VendorAdapter):
    vendor_id = "local"

    def plan(self, payload: dict[str, Any], messages: list[dict[str, Any]],
             ctx: PlanContext) -> RequestPlan:
        plan = RequestPlan(vendor="local", vendor_label=self.profile.label,
                           est_tokens=estimate_tokens(messages))
        plan.cache_engages = False
        plan.notes.append("本地引擎不按 token 计费")
        plan.actions.append("走 OpenAI 兼容端点以支持多轮工具历史重放")
        return _finalise(plan)


_ADAPTERS: dict[str, type[VendorAdapter]] = {
    "deepseek": DeepSeekAdapter,
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "gemini": GeminiAdapter,
    "bailian": BailianAdapter,
    "zhipu": ZhipuAdapter,
    "kimi": KimiAdapter,
    "local": LocalAdapter,
    "generic": ConservativeAdapter,
}


def adapter_for(profile: VendorProfile) -> VendorAdapter:
    """Adapter for a profile. Vendors without a dedicated class get the
    conservative base — never a silent assumption that they behave like
    somebody else."""
    cls = _ADAPTERS.get(profile.id, ConservativeAdapter)
    return cls(profile)


def adapter_for_id(vendor_id: str) -> VendorAdapter:
    return adapter_for(PROFILES.get(vendor_id, PROFILES["generic"]))


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def compare_plans(payload: dict[str, Any], messages: list[dict[str, Any]],
                  contexts: dict[str, PlanContext]) -> list[dict[str, Any]]:
    """Run every vendor's adapter over the same request.

    This is the answer to "mixed fleet should be optimal *per vendor*": the
    same conversation produces a different — each locally optimal — plan and
    price on every vendor, and the caller can compare them side by side.
    """
    rows: list[dict[str, Any]] = []
    for vendor_id, ctx in contexts.items():
        adapter = adapter_for(ctx.profile)
        plan = adapter.plan(payload, messages, ctx)
        row = plan.to_raw()
        row["breakeven_calls"] = breakeven_calls(ctx.profile)
        rows.append(row)
    # 不适用（本地引擎、无价模型）的 est_cost 为 0，排在最后而不是最前：
    # 把「不适用」显在「最便宜」之前会让人误以为它是优势项。
    return sorted(rows, key=lambda r: (r["est_cost"] if r["est_cost"] > 0 else float("inf")))


__all__ = [
    "AnthropicAdapter",
    "BailianAdapter",
    "ConservativeAdapter",
    "DeepSeekAdapter",
    "GeminiAdapter",
    "KimiAdapter",
    "LocalAdapter",
    "OpenAIAdapter",
    "PlanContext",
    "RequestPlan",
    "VendorAdapter",
    "ZhipuAdapter",
    "adapter_for",
    "adapter_for_id",
    "breakeven_calls",
    "compare_plans",
    "ttl_choice",
]
