"""Offline assertions for the per-vendor adapters and the fleet cache layer.

Run directly::

    python -m forge.test_adapters

No network, no model calls. Every economic claim the adapters make
(break-even counts, TTL choices, hop costs) gets a number to check against.
"""

from __future__ import annotations

from .adapters import (
    BailianAdapter,
    GeminiAdapter,
    PlanContext,
    adapter_for,
    breakeven_calls,
    compare_plans,
    ttl_choice,
)
from .cache_state import CacheWarmth, prefix_fingerprint
from .vendors import PROFILES

_PASS: list[str] = []
_FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (_PASS if ok else _FAIL).append(name)
    mark = "ok  " if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return ok


def _payload(system_chars: int = 4000, tools: int = 2, user: str = "hi") -> dict:
    return {
        "system": "S" * system_chars,
        "tools": [{"name": f"t{i}", "description": "d", "input_schema": {}}
                  for i in range(tools)],
        "messages": [{"role": "user", "content": user}],
    }


def _ctx(vendor: str, **kw) -> PlanContext:
    profile = PROFILES[vendor]
    base = {"base_input": 1.08, "base_output": 4.32}
    base.update(kw)
    return PlanContext(profile=profile, **base)


# --------------------------------------------------------------------------- #
# break-even maths
# --------------------------------------------------------------------------- #

def test_breakeven() -> None:
    print("\n-- 回本点计算 --")
    # Anthropic 5m: w=1.25 h=0.10 → (1.25-0.10)/(1-0.10)=1.278 → N>=2
    check("anthropic breakeven=2", breakeven_calls(PROFILES["anthropic"]) == 2,
          str(breakeven_calls(PROFILES["anthropic"])))
    # Bailian explicit: w=1.25 h=0.10 → 2
    check("bailian breakeven=2", breakeven_calls(PROFILES["bailian"]) == 2)
    # DeepSeek: w=1.0 h=0.02 → (1.0-0.02)/(1-0.02)=1.0 → 严格不等式下 N>1 → 2
    # （首次调用写入价等于不缓存，是打平而非胜过，所以不算回本。）
    check("deepseek breakeven=2", breakeven_calls(PROFILES["deepseek"]) == 2,
          str(breakeven_calls(PROFILES["deepseek"])))
    check("zhipu breakeven=2 (free write ties at 1)",
          breakeven_calls(PROFILES["zhipu"]) == 2)
    # 完全没有折扣的厂商永远不回本
    check("generic never breaks even", breakeven_calls(PROFILES["generic"]) >= 10 ** 8)


def test_ttl() -> None:
    print("\n-- TTL 选择 --")
    a = PROFILES["anthropic"]
    check("tight cadence -> 5m", ttl_choice(a, 60) == "5m")
    check("wide cadence -> 1h", ttl_choice(a, 600) == "1h", ttl_choice(a, 600))
    check("disk vendor keeps disk", ttl_choice(PROFILES["deepseek"], 60) == "disk")


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #

def test_adapters() -> None:
    print("\n-- 适配器决策 --")
    p = _payload()

    # DeepSeek: automatic, never marks, but flags off-peak opportunity + think cap
    ds = adapter_for(PROFILES["deepseek"]).plan(p, p["messages"], _ctx("deepseek"))
    check("deepseek: no marker", ds.cache_control is False)
    check("deepseek: cache engages", ds.cache_engages is True)
    check("deepseek: reasoning budget set", ds.reasoning_budget == 4096)
    check("deepseek: saves vs no-cache", ds.est_cost < ds.baseline_cost,
          f"{ds.est_cost} vs {ds.baseline_cost}")
    check("deepseek: est_tokens includes system prompt", ds.est_tokens > 1500,
          str(ds.est_tokens))

    # Anthropic: marker on, two breakpoints (tools + system)
    an = adapter_for(PROFILES["anthropic"]).plan(p, p["messages"], _ctx("anthropic"))
    check("anthropic: marks the wire", an.cache_control is True)
    check("anthropic: two breakpoints", an.breakpoints == 2, str(an.breakpoints))
    check("anthropic: ttl chosen", an.ttl in ("5m", "1h"), an.ttl)

    # 1 call only -> caching cannot pay for itself, adapter says so
    an1 = adapter_for(PROFILES["anthropic"]).plan(
        p, p["messages"], _ctx("anthropic", calls_expected=1))
    check("anthropic: 1 call -> no cache", an1.cache_control is False, an1.actions[0] if an1.actions else "")

    # Bailian: mode flips at 4 calls (explicit vs implicit break-even)
    bl = BailianAdapter(PROFILES["bailian"])
    check("bailian: 3 calls -> implicit", bl.choose_mode(_ctx("bailian", calls_expected=3)) == "implicit")
    check("bailian: 4 calls -> explicit", bl.choose_mode(_ctx("bailian", calls_expected=4)) == "explicit")
    bl3 = bl.plan(p, p["messages"], _ctx("bailian", calls_expected=3))
    bl4 = bl.plan(p, p["messages"], _ctx("bailian", calls_expected=4))
    check("bailian: 3 calls no marker", bl3.cache_control is False)
    check("bailian: 4 calls marker on", bl4.cache_control is True)

    # Gemini: 4096 floor — a short prefix cannot cache, and says so
    short = _payload(system_chars=500)
    gm_short = GeminiAdapter(PROFILES["gemini"]).plan(short, short["messages"], _ctx("gemini"))
    check("gemini: short prefix -> cache miss declared", gm_short.cache_engages is False,
          gm_short.actions[0] if gm_short.actions else "")
    long = _payload(system_chars=30000)
    gm_long = GeminiAdapter(PROFILES["gemini"]).plan(long, long["messages"], _ctx("gemini"))
    check("gemini: long prefix -> cache engages", gm_long.cache_engages is True,
          f"{gm_long.est_tokens} tok")

    # Gemini cliff
    huge = _payload(system_chars=1_200_000)
    gm_huge = GeminiAdapter(PROFILES["gemini"]).plan(huge, huge["messages"], _ctx("gemini"))
    check("gemini: cliff detected", gm_huge.cliff_action == "compact",
          f"est={gm_huge.est_tokens:,} action={gm_huge.cliff_action}")

    # Unknown vendor: never invents a discount
    unk = adapter_for(PROFILES["generic"]).plan(p, p["messages"], _ctx("generic"))
    check("generic: no marker", unk.cache_control is False)
    check("generic: action explains", any("未识别" in a for a in unk.actions), str(unk.actions))

    # Zhipu think cap
    zp = adapter_for(PROFILES["zhipu"]).plan(p, p["messages"], _ctx("zhipu"))
    check("zhipu: reasoning budget set", zp.reasoning_budget == 4096)

    # MiMo: now engages cache with the deepest multiplier in the fleet
    mm = adapter_for(PROFILES["mimo"]).plan(p, p["messages"], _ctx("mimo"))
    check("mimo: cache engages", mm.cache_engages is True)
    check("mimo: saves against no-cache", mm.est_cost < mm.baseline_cost,
          f"{mm.est_cost} vs {mm.baseline_cost}")
    check("mimo: never marks the wire (automatic)", mm.cache_control is False)

    # Still-unknown vendors must not engage
    for vid in ("volcengine", "stepfun"):
        pl = adapter_for(PROFILES[vid]).plan(p, p["messages"], _ctx(vid))
        check(f"{vid}: does not engage cache", pl.cache_engages is False,
              pl.actions[0] if pl.actions else "")

    # Local: no billing at all
    lc = adapter_for(PROFILES["local"]).plan(p, p["messages"], _ctx("local"))
    check("local: cache not applicable", lc.cache_engages is False)


def test_peak_deferral() -> None:
    print("\n-- 峰谷与延后建议 --")
    import calendar

    peak = calendar.timegm((2026, 10, 6, 2, 0, 0, 0, 0, 0))   # 周二 UTC02
    p = _payload()
    plan = adapter_for(PROFILES["deepseek"]).plan(
        p, p["messages"], _ctx("deepseek", when=peak, interactive=False))
    check("deepseek: advises defer in peak", plan.defer is True, str(plan.actions))
    plan_i = adapter_for(PROFILES["deepseek"]).plan(
        p, p["messages"], _ctx("deepseek", when=peak, interactive=True))
    check("deepseek: no defer advice when interactive", plan_i.defer is False)


# --------------------------------------------------------------------------- #
# cross-vendor comparison — the "mixed fleet, per-vendor optimal" claim
# --------------------------------------------------------------------------- #

def test_compare() -> None:
    print("\n-- 混排：同一条请求，各自局部最优 --")
    import calendar

    # 固定在一个**低谷**时刻，否则这条断言会随时钟摆动：
    # DeepSeek 是唯一有峰谷价的厂商，跑在高峰窗口时成本会翻倍，
    # 于是「谁最便宜」的排名会变——实测 2026-10-07 09:33 CST（UTC 01:33）
    # 就落在 DeepSeek 高峰段里，导致这条断言假失败。测试必须控时。
    off_peak = calendar.timegm((2026, 10, 7, 12, 0, 0, 0, 0, 0))   # 周三 UTC 12:00
    p = _payload()
    contexts = {v: _ctx(v, calls_expected=6, when=off_peak) for v in
                ("deepseek", "anthropic", "openai", "gemini", "bailian", "zhipu", "kimi")}
    # 把本地引擎也放进来：它的成本不适用（0），必须排在最后而不是最前。
    contexts["local"] = _ctx("local", calls_expected=6, when=off_peak)
    rows = compare_plans(p, p["messages"], contexts)

    check("compare returns one row per vendor", len(rows) == 8, str(len(rows)))
    check("applicable rows sorted by cost", all(
        rows[i]["est_cost"] <= rows[i + 1]["est_cost"]
        for i in range(len(rows) - 1)
        if rows[i]["est_cost"] > 0 and rows[i + 1]["est_cost"] > 0))
    check("deepseek is cheapest at off-peak", rows[0]["vendor"] == "deepseek",
          f"{rows[0]['vendor']} {rows[0]['est_cost']:.6f}")
    # 顺手把峰谷对排名的影响也钉住：同一请求放进高峰段，DeepSeek 成本翻倍。
    peak = calendar.timegm((2026, 10, 7, 2, 0, 0, 0, 0, 0))       # 周三 UTC 02:00
    peak_rows = compare_plans(p, p["messages"],
                             {v: _ctx(v, calls_expected=6, when=peak)
                              for v in ("deepseek", "kimi")})
    by_vendor = {r["vendor"]: r["est_cost"] for r in peak_rows}
    offpeak_deepseek = next(r["est_cost"] for r in rows if r["vendor"] == "deepseek")
    check("deepseek doubles inside its peak window",
          abs(by_vendor["deepseek"] - 2 * offpeak_deepseek) < 1e-6,
          f"peak={by_vendor['deepseek']:.6f} vs 2x{offpeak_deepseek:.6f}")
    check("not-applicable sorts last, not first",
          rows[-1]["vendor"] == "local" and rows[-1]["est_cost"] == 0,
          f"{rows[-1]['vendor']} {rows[-1]['est_cost']}")
    check("every row carries actions", all(r["actions"] for r in rows))
    check("explicit vendors marked, implicit not",
          {r["vendor"]: r["cache_control"] for r in rows}["anthropic"] is True
          and {r["vendor"]: r["cache_control"] for r in rows}["deepseek"] is False)


# --------------------------------------------------------------------------- #
# fleet cache warmth
# --------------------------------------------------------------------------- #

def test_warmth() -> None:
    print("\n-- 混排缓存连续性 --")
    store = CacheWarmth()
    p = _payload()
    fp = prefix_fingerprint(p)
    check("fingerprint is stable", fp == prefix_fingerprint(_payload()), fp)
    check("fingerprint ignores history growth",
          fp == prefix_fingerprint({**_payload(), "messages": [{"role": "user", "content": "much longer"}]}))

    check("cold by default", not store.is_warm(PROFILES["anthropic"], fp))
    store.remember("anthropic", fp)
    check("warm after remember", store.is_warm(PROFILES["anthropic"], fp))
    check("warmth is per-vendor", not store.is_warm(PROFILES["deepseek"], fp))

    # unknown-cache vendors are never assumed warm
    store.remember("volcengine", fp)
    check("unknown vendor never warm", not store.is_warm(PROFILES["volcengine"], fp))
    store.remember("stepfun", fp)
    check("stepfun never warm either", not store.is_warm(PROFILES["stepfun"], fp))

    # MiMo 2026-10-07 已核实有缓存口径，所以它**应该**参与温暖度跟踪
    store.remember("mimo", fp)
    check("mimo now tracked (published cache pricing)",
          store.is_warm(PROFILES["mimo"], fp))

    # hop costing: warm vs cold
    cold_hop = store.hop_cost(PROFILES["deepseek"], fp, prefix_tokens=4000, base_input=1.08)
    warm_hop = store.hop_cost(PROFILES["anthropic"], fp, prefix_tokens=4000, base_input=1.08)
    check("cold hop charges full prefix", cold_hop.cold_tokens == 4000)
    check("warm hop charges hit rate only", warm_hop.warm is True and warm_hop.cold_tokens == 0,
          f"warm={warm_hop.warm} cost={warm_hop.cold_cost}")

    # stickiness: the warm vendor should rank first even if listed later
    ranked = store.rank_candidates(
        [("a", PROFILES["deepseek"]), ("b", PROFILES["anthropic"])], fp,
        prefix_tokens=4000, base_input=1.08)
    check("warm vendor ranks first", ranked[0]["vendor"] == "anthropic", str(ranked))
    tip = store.suggest([("a", PROFILES["deepseek"]), ("b", PROFILES["anthropic"])], fp,
                        prefix_tokens=4000, base_input=1.08, current="deepseek")
    check("suggest mentions the warm hop", "anthropic" in tip, tip)

    # expiry: a 5m entry must not look warm after 10 minutes
    now = 1_000_000.0
    store.remember("anthropic", fp, when=now)
    check("expires after ttl", not store.is_warm(PROFILES["anthropic"], fp, when=now + 600))
    check("alive inside ttl", store.is_warm(PROFILES["anthropic"], fp, when=now + 120))

    removed = store.prune(when=now + 100_000, profiles=list(PROFILES.values()))
    check("prune removes expired", removed >= 1, str(removed))


def main() -> int:
    print("forge adapter + fleet-cache assertions")
    test_breakeven()
    test_ttl()
    test_adapters()
    test_peak_deferral()
    test_compare()
    test_warmth()
    total = len(_PASS) + len(_FAIL)
    print(f"\n{len(_PASS)}/{total} passed")
    if _FAIL:
        print("failed: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
