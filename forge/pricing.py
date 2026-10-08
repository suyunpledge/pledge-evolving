"""Cost accounting, so provider choice is decided by the invoice, not by vibes.

Two facts from real invoices (2026-09-14) that this module encodes as *effective
blended rates*:

  DeepSeek    ¥203.00 / 1,273,002,379 tokens  ≈ ¥0.1595 per 1M tokens
  MiMo        ¥ 48.00 /   636,744,162 tokens  ≈ ¥0.0754 per 1M tokens

Blended is the honest unit for comparing "the same job on two providers": agent
workloads are cache-heavy and every provider discounts cache hits differently,
so a per-token list price is a worse predictor than the rate you actually paid.
The table below is seeded from those invoices and is meant to be re-seeded as
new invoices arrive — it is data, not doctrine.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# model -> (currency, blended ¥ per 1M tokens, note)
# 未知价 = 0.0 的单一权威定义（R4-3）：0.0 在不同消费点有相反的解读 —
#   routing._sorted_by_cost 取保守解释（未知价 = 最贵，排最后，防止免费档被打穿）；
#   loop._pricing_scale 取宽松解释（未知价 = 不放宽预算，返回 1.0 地板）。
#   两处都已注释回指本表；改语义先改这里。
EFFECTIVE_RATES: dict[str, tuple[str, float, str]] = {
    "deepseek-flash": ("CNY", 0.1595, "由 2026-09-14 账单反推（金额从略）"),
    "deepseek-v4-pro": ("CNY", 0.1595, "同上账单未拆分模型，暂共用同一有效单价"),
    "mimo-v2.5": ("CNY", 0.0754, "由 2026-09-14 账单反推（金额从略）"),
    "mimo-v2.5-pro": ("CNY", 0.0754, "同上账单未拆分型号，暂共用同一有效单价"),

    # V2.6 系列（2026-09-23 上线）：能力对标 K3/GLM5.3/Qwen3.8Max，单价未变，
    # 沿用 9/14 账单反推的 mimo 混合有效单价；三型号账单未拆分，共用同一行。
    "mimo-v2.6-flash": ("CNY", 0.0754, "V2.6 同价（2026-09-23 用户确认），沿用 mimo 混合有效单价"),
    "mimo-v2.6-pro": ("CNY", 0.0754, "同 mimo-v2.6-flash（V2.6 三型号账单未拆分）"),
    "mimo-v2.6-pro-ultraspeed": ("CNY", 0.0754, "同 mimo-v2.6-flash（V2.6 三型号账单未拆分）"),
    # R1-1：claude-sonnet-5 在 forge 语境 = 环回网关的广告名，网关
    # 把它翻译成 mimo-v2.5 上游（gateway --model-map claude-sonnet-5=mimo-v2.5），
    # 上面那条账单正是经此链路产生的 → 有效单价同 mimo。0.0（审查档按次
    # 计费）只适用于 claude-opus-5 这类真审查档。
    "claude-sonnet-5": ("CNY", 0.0754, "环回网关广告名，翻译到 mimo-v2.5 上游；同 MiMo 账单反推"),
    "claude-opus-5": ("CNY", 0.0, "审查档：按次调用，未纳入有效单价统计"),
}

MILLION = 1_000_000.0

# --------------------------------------------------------------------------- #
# 目录价（非混合价）：计费需要区分命中 / 未命中 / 写入 / 输出四类，
# 单一混合价无法回答"这次改动的收益是多少"。这里按厂商官方公布口径存目录价，
# 单位统一为 CNY / 1M tokens（USD 按 7.2 折算，与账单口径一致）。
# 未列出的模型回落到 EFFECTIVE_RATES 的混合价——宁可给一个粗略值，
# 也不要因为表里没有就假装成本是 0。
# --------------------------------------------------------------------------- #
BASE_RATES: dict[str, dict[str, float]] = {
    # input = 未命中输入；output = 输出；cached/write 由厂商倍率在运行时推出
    "deepseek-flash":       {"input": 1.08, "output": 4.32, "cache_write": 1.08},
    "deepseek-v4-pro":      {"input": 4.75, "output": 14.26, "cache_write": 4.75},
    "glm-5.3":              {"input": 8.0,  "output": 28.0, "cache_write": 8.0},
    "glm-5.3-flash":        {"input": 0.8,  "output": 2.8,  "cache_write": 0.8},
    "kimi-k3":              {"input": 20.0, "output": 100.0, "cache_write": 20.0},
    "kimi-k2.6":            {"input": 6.5,  "output": 27.0, "cache_write": 6.5},
    "claude-sonnet-5.5":    {"input": 14.4, "output": 72.0, "cache_write": 18.0},
    "claude-opus-5.5":      {"input": 28.8, "output": 144.0, "cache_write": 36.0},
    "gpt-6.1-sol":          {"input": 14.4, "output": 72.0, "cache_write": 18.0},
    "gpt-6-luna":           {"input": 0.72, "output": 3.6,  "cache_write": 0.9},
    # 2026-10-07 补：向导 / CLI 里用的**连字符**型号名。
    # 同一份已核实目录价，只是写法不同（claude-sonnet-5-5 vs claude-sonnet-5.5）。
    # 不补的话 rate 查不到 → 价格 0 → 适配器会直接放弃缓存决策。
    "claude-haiku-4-5":     {"input": 7.2,  "output": 36.0, "cache_write": 9.0},
    "claude-sonnet-5-5":    {"input": 14.4, "output": 72.0, "cache_write": 18.0},
    "claude-opus-5-5":      {"input": 28.8, "output": 144.0, "cache_write": 36.0},
    "gpt-6-astra":          {"input": 72.0, "output": 360.0, "cache_write": 90.0},
    "gemini-3.8-flash":     {"input": 5.4,  "output": 27.0, "cache_write": 5.4},
    "gemini-3.1-pro":       {"input": 14.4, "output": 86.4, "cache_write": 14.4},
}

# 写法别名：连字符型 ↔ 点号型。同一模型的两种常见命名。
_RATE_ALIASES: dict[str, str] = {
    "claude-sonnet-5-5": "claude-sonnet-5.5",
    "claude-opus-5-5": "claude-opus-5.5",
}

# 价格未知时的中性单位价。适配器的缓存决策是**比例**决策（尺度无关），
# 给 1.0 能让决策与命中／未命中倍率照常生效，而不会因为“查不到价”
# 就把整个缓存策略丢掉。金额绝对值由调用方按 estimated 标记自行处理。
UNKNOWN_PRICE = 1.0

# 厂商倍率的兜底值（画像不可用时的保守估计：不打折）
_DEFAULT_CACHE_READ = 1.0
_DEFAULT_CACHE_WRITE = 1.0


def base_rate(model: str) -> dict[str, float] | None:
    """目录价，找不到返回 None（调用方回落到混合价或单位价）。"""
    name = str(model)
    row = BASE_RATES.get(name)
    if row is None:
        canonical = _RATE_ALIASES.get(name)
        if canonical:
            row = BASE_RATES.get(canonical)
    return row


def index_price(model: str) -> tuple[float, float, bool]:
    """给适配器用的（输入单价, 输出单价, 是否真实目录价）。

    三级回落：目录价 → 账单反推的混合有效单价 → 中性单位价。
    最后一级是必要的：适配器的缓存决策是比例决策，不该因为“没查到价”
    就整个放弃；返回的 ``known=False`` 让调用方知道金额只能当比例看。
    """
    rates = base_rate(model)
    if rates:
        return float(rates["input"]), float(rates["output"]), True
    blended = rate_for(model)
    if blended > 0:
        return float(blended), float(blended), False
    return UNKNOWN_PRICE, UNKNOWN_PRICE, False


def cost_of_usage(
    model: str,
    *,
    input_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int = 0,
    profile=None,
    peak: bool = False,
    batch: bool = False,
    flex: bool = False,
    long_context: bool = False,
) -> dict[str, float]:
    """按厂商计费结构算一次调用的钱（CNY）。

    ``profile`` 是 vendors.VendorProfile（可选）。有画像时用它的命中/写入倍率
    （这才是"同一个请求在不同厂商值多少钱"的答案）；没有时按不打折保守估计。
    ``peak/batch/flex/long_context`` 是四个修饰开关，与 vendors.price_multiplier 同源。

    返回分项明细，便于在运行报告里解释"钱花在哪"。
    """
    rates = base_rate(model)
    if rates is None:
        blended = rate_for(model)
        if blended <= 0:
            return {"input": 0.0, "cached": 0.0, "write": 0.0, "output": 0.0,
                    "total": 0.0, "estimated": 1.0}
        return {"input": round(blended * input_tokens / MILLION, 6),
                "cached": 0.0, "write": 0.0,
                "output": round(blended * output_tokens / MILLION, 6),
                "total": round(blended * (input_tokens + output_tokens) / MILLION, 6),
                "estimated": 1.0}

    read_mult = getattr(profile, "cache_read_multiplier", _DEFAULT_CACHE_READ)
    write_mult = getattr(profile, "cache_write_multiplier", _DEFAULT_CACHE_WRITE)

    modifier = 1.0
    if peak:
        modifier *= 2.0
    if batch:
        modifier *= 0.5
    if flex:
        modifier *= 0.5
    if long_context:
        modifier *= 2.0

    hits = max(0, int(cached_tokens))
    writes = max(0, int(cache_write_tokens))
    misses = max(0, int(input_tokens) - hits - writes)

    p_input = rates["input"] * modifier
    p_read = rates["input"] * read_mult * modifier
    p_write = rates.get("cache_write", rates["input"]) * write_mult * modifier
    p_output = rates["output"] * modifier

    parts = {
        "input": round(p_input * misses / MILLION, 6),
        "cached": round(p_read * hits / MILLION, 6),
        "write": round(p_write * writes / MILLION, 6),
        "output": round(p_output * int(output_tokens) / MILLION, 6),
        "estimated": 0.0,
    }
    parts["total"] = round(sum(parts[k] for k in ("input", "cached", "write", "output")), 6)
    return parts


def cost_of_usage_simple(model: str, usage) -> float:
    """从 model.Usage 对象直接算总成本（CNY）的便利入口。"""
    parts = cost_of_usage(
        model,
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        cached_tokens=int(getattr(usage, "cached_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )
    return parts["total"]


def rate_for(model: str) -> float:
    row = EFFECTIVE_RATES.get(str(model))
    return float(row[1]) if row else 0.0


def cost_of(model: str, total_tokens: int) -> float:
    """¥ for a given token count at the model's effective blended rate."""
    return round(rate_for(model) * (max(int(total_tokens), 0) / MILLION), 6)


@dataclass
class SpendEntry:
    model: str
    tokens: int
    cost: float
    at: float = field(default_factory=time.time)
    note: str = ""

    def to_raw(self) -> dict[str, Any]:
        return {"model": self.model, "tokens": self.tokens, "cost": self.cost,
                "at": self.at, "note": self.note}


class CostLedger:
    """Append-only spend log + the comparison we actually care about."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[SpendEntry] = []
        if self.path and self.path.is_file():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                self.entries.append(SpendEntry(model=str(raw.get("model", "")),
                                               tokens=int(raw.get("tokens", 0)),
                                               cost=float(raw.get("cost", 0.0)),
                                               at=float(raw.get("at", time.time())),
                                               note=str(raw.get("note", ""))))

    def record(self, model: str, tokens: int, *, note: str = "") -> SpendEntry:
        from .secrets import redact
        model, note = redact(model), redact(note)
        entry = SpendEntry(model=model, tokens=int(tokens), cost=cost_of(model, tokens), note=note)
        self.entries.append(entry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_raw(), ensure_ascii=False) + "\n")
        return entry

    def record_session(self, session, model: str) -> SpendEntry | None:
        """Pull the token rollup out of a session's event log."""
        tokens = int((session.tokens() or {}).get("total", 0)) if session is not None else 0
        if tokens <= 0:
            return None
        return self.record(model, tokens, note=f"session {getattr(session.meta, 'get', lambda *_: '')('session_id')}")

    # -- reporting -------------------------------------------------------
    def totals(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for entry in self.entries:
            row = out.setdefault(entry.model, {"tokens": 0.0, "cost": 0.0, "calls": 0.0})
            row["tokens"] += entry.tokens
            row["cost"] += entry.cost
            row["calls"] += 1
        for row in out.values():
            row["cost"] = round(row["cost"], 6)
        return out

    def compare(self, left: str, right: str, tokens: int) -> dict[str, Any]:
        """What the same job costs on two providers, and the saving."""
        left_cost, right_cost = cost_of(left, tokens), cost_of(right, tokens)
        cheaper = left if left_cost <= right_cost else right
        saving = abs(left_cost - right_cost)
        pct = (saving / max(left_cost, right_cost) * 100) if max(left_cost, right_cost) else 0.0
        return {
            "tokens": int(tokens),
            "left": {"model": left, "cost": left_cost},
            "right": {"model": right, "cost": right_cost},
            "cheaper": cheaper,
            "saving": round(saving, 6),
            "saving_pct": round(pct, 1),
        }

    def to_raw(self) -> dict[str, Any]:
        return {"entries": len(self.entries), "totals": self.totals(),
                "rates": {k: {"currency": v[0], "per_million": v[1], "note": v[2]}
                          for k, v in EFFECTIVE_RATES.items()}}


def compare_models(tokens: int, models: Iterable[str]) -> list[dict[str, Any]]:
    rows = [{"model": m, "tokens": int(tokens), "cost": cost_of(m, tokens), "rate": rate_for(m)}
            for m in models]
    return sorted(rows, key=lambda row: row["cost"])


__all__ = [
    "BASE_RATES",
    "CostLedger",
    "EFFECTIVE_RATES",
    "MILLION",
    "SpendEntry",
    "UNKNOWN_PRICE",
    "base_rate",
    "compare_models",
    "cost_of",
    "cost_of_usage",
    "cost_of_usage_simple",
    "index_price",
    "rate_for",
]
