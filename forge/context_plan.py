"""Long-context orchestration — the lossless / compressed decision.

The problem
-----------
Two vendors sell context as a **cliff**: OpenAI and Gemini double every rate
once the prompt crosses a threshold (long context). Every other vendor sells it
as a **slope** (you just pay for the tokens). The framework therefore has two
very different failure modes:

* On a cliff vendor, sending 200,001 tokens costs twice as much as 199,999 —
  a rounding error in the history can double the invoice.
* On a slope vendor, dropping history to save input tokens can cost *more* in
  output tokens, because the model re-derives what it forgot.

Neither is a technical question with one right answer, which is why the user
decides. What the framework owes the user is an honest number at decision time:
"keeping this costs ¥X more, dropping it costs ¥Y of re-derivation risk".

Modes
-----
``lossless``  never compact. The whole conversation goes to the model. Correct
              choice when the task needs every detail (legal review, long
              refactors) and the vendor has no cliff.
``compact``   compress aggressively to stay under the cliff. Correct choice on
              cliff vendors, and on long-running sessions where the tail
              matters more than the head.
``ask``       surface the decision to the caller. Interactive sessions prompt;
              non-interactive runs fall back to ``default_mode`` and say so in
              the run report rather than silently choosing.

The estimate is deliberately cheap (character count / ratio) because it runs
before every request and must never become the bottleneck.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .vendors import VendorProfile

CONTEXT_MODES: tuple[str, ...] = ("lossless", "compact", "ask")

# Blended characters-per-token. Chinese prose runs ~1.6 chars/token, English
# prose ~4; agent traffic is mixed, so 2.5 is the honest middle and errs
# slightly conservative (an over-estimate triggers compaction early, which is
# cheaper than crossing a cliff).
_CHARS_PER_TOKEN = 2.5

# Keep this much headroom below the cliff when compacting. Crossing by one
# token doubles the bill, so the target is deliberately inside the boundary.
_CLIFF_HEADROOM = 0.85

# A message list shorter than this is never worth compacting: the summary
# costs tokens too, and below the cache minimum it breaks the prefix.
_MIN_COMPACT_CHARS = 6_000


@dataclass(frozen=True)
class ContextDecision:
    action: str            # "pass" | "compact"
    mode: str              # the mode that produced this decision
    estimated_tokens: int
    reason: str
    # Present when a cliff is involved, so the report can quantify it.
    crosses_cliff: bool = False
    cliff_multiplier: float = 1.0

    def to_raw(self) -> dict[str, Any]:
        return {
            "action": self.action, "mode": self.mode,
            "estimated_tokens": self.estimated_tokens, "reason": self.reason,
            "crosses_cliff": self.crosses_cliff,
            "cliff_multiplier": self.cliff_multiplier,
        }


def estimate_tokens(messages: Iterable[dict[str, Any]]) -> int:
    """Cheap token estimate over a message list."""
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", "")))
                else:
                    total += len(str(block))
        else:
            total += len(str(content))
    return int(total / _CHARS_PER_TOKEN)


def estimate_text(text: str) -> int:
    """Token estimate for a bare string (system prompt, tool-definition blob).

    Exists so the adapters do not each carry their own copy of the
    characters-per-token constant — that is exactly the kind of duplicate
    constant that drifts and then makes two parts of the framework disagree
    about how big a prompt is.
    """
    return int(len(str(text)) / _CHARS_PER_TOKEN)


def classify(profile: VendorProfile, est_tokens: int) -> tuple[bool, float]:
    """Does this prompt cross the vendor's long-context cliff?"""
    threshold = profile.long_context_threshold
    if not threshold:
        return False, 1.0
    if est_tokens > threshold:
        return True, profile.long_context_multiplier
    return False, 1.0


@dataclass
class ContextPlan:
    """Resolved long-context policy for one run.

    ``mode`` is what the user asked for; ``default_mode`` is what a
    non-interactive run falls back to when ``mode`` is ``ask``.
    """

    mode: str = "ask"
    default_mode: str = "compact"
    keep_tail: int = 6
    explorer: Any = None      # callable(decision) -> None, for the UI to hook

    @staticmethod
    def from_config(cfg) -> "ContextPlan":
        # 被 disabled 的 context 行必须退回默认（ask），而不是把它声明的 mode 当生效值——
        # 否则“禁用上下文策略”反而会变成“强制无损”。
        row_obj = cfg.row("context") if hasattr(cfg, "row") else None
        if row_obj is not None and getattr(row_obj, "disabled", False):
            return ContextPlan(mode="ask", default_mode="compact", keep_tail=6)
        # 行的 config **就是**策略本体（keys: mode / defaultMode / keepTail），
        # 不是嵌在 "policy" 键下；早先按 cfg.get("context", "policy") 读会在
        # 每个向导生成的配置上静默拿到空字典、退回默认值。
        def _read(key: str, default: Any) -> Any:
            return cfg.get("context", key, default)

        return ContextPlan(
            mode=str(_read("mode", "ask")),
            default_mode=str(_read("defaultMode", "compact")),
            keep_tail=int(_read("keepTail", 6) or 6),
        )

    # -- decision ---------------------------------------------------------
    def decide(
        self,
        messages: list[dict[str, Any]],
        profile: VendorProfile,
        *,
        interactive: bool = False,
        base_budget: int = 24_000,
    ) -> ContextDecision:
        est = estimate_tokens(messages)
        crosses, multiplier = classify(profile, est)
        size = sum(len(str(m.get("content", ""))) for m in messages)

        mode = self.mode
        if mode == "ask":
            mode = self._resolve_ask(interactive, crosses, multiplier)

        if mode == "lossless":
            reason = "无损模式：完整上下文送出"
            if crosses:
                reason += f"（已越过 {profile.long_context_threshold:,} token 阶梯，单价 ×{multiplier:g}）"
            return ContextDecision("pass", "lossless", est, reason, crosses, multiplier)

        # compact
        if size <= _MIN_COMPACT_CHARS:
            return ContextDecision("pass", "compact", est,
                                   "压缩模式：上下文过短，压缩收益为负，直接送出",
                                   crosses, multiplier)
        if crosses:
            return ContextDecision("compact", "compact", est,
                                   f"压缩模式：{est:,} token 越过 {profile.long_context_threshold:,} 阶梯，"
                                   f"压缩以避免单价 ×{multiplier:g}", crosses, multiplier)
        return ContextDecision("pass", "compact", est,
                               f"压缩模式：{est:,} token 未越阶梯，无需动手", crosses, multiplier)

    def _resolve_ask(self, interactive: bool, crosses: bool, multiplier: float) -> str:
        """Turn ``ask`` into a concrete mode."""
        if interactive and self.explorer is not None:
            try:
                return str(self.explorer(crosses=crosses, multiplier=multiplier)) or self.default_mode
            except Exception:
                return self.default_mode
        return self.default_mode

    # -- budget integration ------------------------------------------------
    def adjust_budget(self, base_budget: int, profile: VendorProfile,
                      mode: str = "") -> int:
        """Translate the context policy into a compaction ceiling.

        ``compact`` tightens the ceiling to just under the vendor's cliff so the
        compactor fires *before* the rate doubles. ``lossless`` raises it far
        above the current size so the compactor never fires — the cost of doing
        so is exactly what ``decide`` reports.

        2026-10-07 修正：预算是**字符**，阶梯线是 **token**。之前直接拿
        threshold×0.85 当字符用，收窄量少算了 2.5 倍——在长会话里正好会
        把压缩顶到阶梯线以上、白白触发翻倍。必须换算。
        """
        resolved = mode or self.mode
        if resolved == "ask":
            resolved = self.default_mode

        if resolved == "lossless":
            return max(base_budget, 10_000_000)

        threshold = profile.long_context_threshold
        if threshold:
            cliff_chars = int(threshold * _CLIFF_HEADROOM * _CHARS_PER_TOKEN)
            return max(2_000, min(base_budget, cliff_chars))
        return base_budget


def prompt_context_choice(profile: VendorProfile, est_tokens: int) -> str:
    """Interactive prompt used by the setup wizard and by ``--context-policy ask``.

    Returns ``"lossless"`` or ``"compact"``.
    """
    crosses, multiplier = classify(profile, est_tokens)
    print("\n长上下文编排策略")
    print(f"  当前预估 {est_tokens:,} token；厂商 {profile.label} "
          + (f"阶梯线 {profile.long_context_threshold:,}（超出单价 ×{multiplier:g}）"
             if profile.long_context_threshold else "无阶梯线（按量线性计费）"))
    print("  [1] 无损输入 — 上下文完整保留，成本更高，信息不丢失")
    print("  [2] 压缩输入 — 自动压缩早期内容，省钱，但可能丢失细节")
    while True:
        choice = input("  选择（1/2，默认 2）> ").strip()
        if choice in ("", "2"):
            return "compact"
        if choice == "1":
            return "lossless"
        print("  无效输入，请输入 1 或 2")


__all__ = [
    "CONTEXT_MODES",
    "ContextDecision",
    "ContextPlan",
    "classify",
    "estimate_text",
    "estimate_tokens",
    "prompt_context_choice",
]
