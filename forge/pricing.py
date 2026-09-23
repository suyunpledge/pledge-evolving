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
    "CostLedger",
    "EFFECTIVE_RATES",
    "MILLION",
    "SpendEntry",
    "compare_models",
    "cost_of",
    "rate_for",
]
