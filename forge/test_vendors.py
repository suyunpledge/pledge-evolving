"""Offline assertions for the vendor adaptation layer and context policy.

Run directly::

    python -m forge.test_vendors

Self-contained (no network, no model calls), in the same spirit as the
``selftest.py`` cache assertions: every claim the framework makes about a
vendor's billing shape gets a number it can be checked against.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .context_plan import ContextPlan, classify, estimate_tokens
from .pricing import cost_of_usage
from .vendors import (
    PROFILES,
    describe_fleet,
    hit_rate,
    is_peak,
    normalize_usage,
    price_multiplier,
    profile_for,
    shape_anthropic,
    shape_bailian,
    should_inject_cache,
)

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


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

def test_detection() -> None:
    print("\n-- 厂商识别 --")
    check("url:deepseek",
          profile_for(base_url="https://api.deepseek.com").id == "deepseek")
    check("url:anthropic",
          profile_for(base_url="https://api.anthropic.com").id == "anthropic")
    check("url:bailian",
          profile_for(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1").id == "bailian")
    check("url:zhipu",
          profile_for(base_url="https://open.bigmodel.cn/api/paas/v4").id == "zhipu")
    check("model:kimi",
          profile_for(base_url="http://localhost:9999", model="kimi-k3").id == "kimi")
    check("url:mimo (real fleet)",
          profile_for(base_url="https://api.xiaomimimo.com/v1", model="mimo-v2.6-flash").id == "mimo")
    check("url:minimax (real fleet)",
          profile_for(base_url="https://api.minimaxi.com/v1", model="minimax-m3").id == "minimax")
    check("url:volcengine by domain",
          profile_for(base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
                      model="doubao-seed-evolving").id == "volcengine")
    check("model:stepfun by prefix",
          profile_for(base_url="https://api.stepfun.com/step_plan/v1",
                      model="step-3.5-flash-2603").id == "stepfun")
    check("service:local-wins-over-url",
          profile_for(base_url="https://api.deepseek.com", service="ollama",
                      model="qwen3:8b").id == "local")
    check("explicit:overrides-everything",
          profile_for(base_url="https://api.deepseek.com", vendor="anthropic").id == "anthropic")
    check("unknown:generic",
          profile_for(base_url="https://example.invalid/v1").id == "generic")


# --------------------------------------------------------------------------- #
# usage normalisation
# --------------------------------------------------------------------------- #

def test_usage() -> None:
    print("\n-- usage 归一 --")

    ds = normalize_usage(PROFILES["deepseek"], {
        "prompt_tokens": 1000, "completion_tokens": 100,
        "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100,
    })
    check("deepseek: cached", ds.get("cached_tokens") == 900, str(ds))
    check("deepseek: input=hit+miss", ds.get("input_tokens") == 1000, str(ds))
    check("deepseek: output", ds.get("output_tokens") == 100)
    check("deepseek: hit_rate 0.9", abs(hit_rate(ds) - 0.9) < 1e-9)

    oa = normalize_usage(PROFILES["openai"], {
        "prompt_tokens": 1000, "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 800},
    })
    check("openai: nested cached", oa.get("cached_tokens") == 800, str(oa))
    check("openai: input", oa.get("input_tokens") == 1000)

    an = normalize_usage(PROFILES["anthropic"], {
        "input_tokens": 12, "output_tokens": 30,
        "cache_read_input_tokens": 700, "cache_creation_input_tokens": 300,
    })
    check("anthropic: read->cached", an.get("cached_tokens") == 700, str(an))
    check("anthropic: creation->write", an.get("cache_write_tokens") == 300)

    gm = normalize_usage(PROFILES["gemini"], {"total_cached_tokens": 512,
                                              "promptTokenCount": 1000})
    check("gemini: total_cached_tokens", gm.get("cached_tokens") == 512)

    rk = normalize_usage(PROFILES["deepseek"], {"reasoning_tokens": 42, "prompt_tokens": 10})
    check("reasoning normalized", rk.get("reasoning_tokens") == 42)

    check("empty usage tolerated", normalize_usage(PROFILES["openai"], {}) == {})
    check("none usage tolerated", normalize_usage(PROFILES["openai"], None) == {})


# --------------------------------------------------------------------------- #
# peak / valley
# --------------------------------------------------------------------------- #

def test_peak() -> None:
    print("\n-- 峰谷时段 --")
    ds = PROFILES["deepseek"]
    # 2026-10-06 是周二。UTC 02:00 = 峰，UTC 12:00 = 谷。
    import calendar
    import time as _t

    def utc(y, mo, d, h):
        return calendar.timegm((y, mo, d, h, 0, 0, 0, 0, 0))

    tue_peak = utc(2026, 10, 6, 2)
    tue_valley = utc(2026, 10, 6, 12)
    sat = utc(2026, 10, 10, 2)

    check("deepseek: 周二 UTC02 = 峰", is_peak(ds, tue_peak))
    check("deepseek: 周二 UTC12 = 谷", not is_peak(ds, tue_valley))
    check("deepseek: 周六 UTC02 = 谷", not is_peak(ds, sat))
    check("deepseek: 节假日=谷", not is_peak(ds, tue_peak, holiday=True))
    check("openai: 无峰谷", not is_peak(PROFILES["openai"], tue_peak))

    check("multiplier: 峰=2x", abs(price_multiplier(ds, when=tue_peak) - 2.0) < 1e-9)
    check("multiplier: 谷=1x", abs(price_multiplier(ds, when=tue_valley) - 1.0) < 1e-9)
    check("multiplier: 谷+batch(不支持)=1x",
          abs(price_multiplier(ds, when=tue_valley, batch=True) - 1.0) < 1e-9)
    check("multiplier: openai+batch=0.5x",
          abs(price_multiplier(PROFILES["openai"], batch=True) - 0.5) < 1e-9)
    check("multiplier: gemini 超 200K =2x",
          abs(price_multiplier(PROFILES["gemini"], prompt_tokens=250_000) - 2.0) < 1e-9)
    check("multiplier: gemini 未超 =1x",
          abs(price_multiplier(PROFILES["gemini"], prompt_tokens=100_000) - 1.0) < 1e-9)


# --------------------------------------------------------------------------- #
# request shaping
# --------------------------------------------------------------------------- #

def test_shaping() -> None:
    print("\n-- 请求整形 --")
    check("anthropic: inject by default",
          should_inject_cache(PROFILES["anthropic"]) is True)
    check("deepseek: no injection",
          should_inject_cache(PROFILES["deepseek"]) is False)
    check("override off", should_inject_cache(PROFILES["anthropic"], "off") is False)
    check("override explicit on implicit vendor",
          should_inject_cache(PROFILES["deepseek"], "explicit") is True)

    payload = {
        "model": "claude-sonnet-5",
        "system": "you are a careful assistant",
        "tools": [{"name": "a"}, {"name": "b"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = shape_anthropic(payload, PROFILES["anthropic"])
    check("anthropic: tools[-1] marked",
          out["tools"][-1].get("cache_control", {}).get("type") == "ephemeral")
    check("anthropic: tools[0] untouched", "cache_control" not in out["tools"][0])
    check("anthropic: system converted to blocks",
          isinstance(out["system"], list)
          and out["system"][-1].get("cache_control", {}).get("type") == "ephemeral")
    check("anthropic: messages untouched",
          out["messages"][0]["content"] == "hi")

    ttl_payload = {"system": "s", "tools": [{"name": "a"}], "messages": []}
    out_ttl = shape_anthropic(ttl_payload, PROFILES["anthropic"], ttl="1h")
    check("anthropic: 1h ttl marker",
          out_ttl["system"][-1]["cache_control"].get("ttl") == "1h")

    bl = {"messages": [{"role": "system", "content": "sys"},
                       {"role": "user", "content": "q"}]}
    out_bl = shape_bailian(bl, PROFILES["bailian"])
    # 2026-10-07：标记必须落在**稳定的 system 消息**上。
    # 旧实现标最后一条消息 = 每轮都变的用户输入 → 每次都写新缓存、永不命中，
    # 比不开缓存还贵。
    check("bailian: marks the stable system message",
          out_bl["messages"][0]["content"][-1]["cache_control"]["type"] == "ephemeral")
    check("bailian: leaves the changing user tail alone",
          out_bl["messages"][-1]["content"] == "q")

    no_sys = {"messages": [{"role": "user", "content": "q"}]}
    out_no = shape_bailian(no_sys, PROFILES["bailian"])
    check("bailian: no system message -> no-op",
          out_no["messages"][0]["content"] == "q")


# --------------------------------------------------------------------------- #
# fleet semantics
# --------------------------------------------------------------------------- #

def test_fleet() -> None:
    print("\n-- fleet 单厂家 / 混排 --")
    single = describe_fleet([PROFILES["deepseek"], PROFILES["deepseek"]])
    check("single: mode", single["mode"] == "single", str(single["mode"]))
    check("single: cache shared", single["cache_shared"] is True)

    mixed = describe_fleet([PROFILES["zhipu"], PROFILES["deepseek"], PROFILES["anthropic"]])
    check("mixed: mode", mixed["mode"] == "mixed")
    check("mixed: cache NOT shared", mixed["cache_shared"] is False)
    check("mixed: best cache 0.02", abs(mixed["best_cache_read_multiplier"] - 0.02) < 1e-9)

    with_local = describe_fleet([PROFILES["deepseek"], PROFILES["local"]])
    check("local does not make it mixed", with_local["mode"] == "single")


# --------------------------------------------------------------------------- #
# context policy
# --------------------------------------------------------------------------- #

def test_context() -> None:
    print("\n-- 长上下文编排 --")
    msgs = [{"role": "user", "content": "x" * 10_000}]
    est = estimate_tokens(msgs)
    check("estimate 10000 chars ~ 4000 tok", 3900 <= est <= 4100, str(est))

    crosses, mult = classify(PROFILES["gemini"], 250_000)
    check("gemini 250k crosses", crosses and abs(mult - 2.0) < 1e-9)
    check("deepseek never crosses", classify(PROFILES["deepseek"], 5_000_000)[0] is False)

    # 600k 字符 ≈ 240k token，越过 Gemini 的 200k 阶梯线。
    # （早先版本用了 100k 字符，只有 40k token，根本没越线，导致断言假失败。）
    deep = [{"role": "user", "content": "x" * 600_000}]
    check("estimate of deep prompt crosses",
          estimate_tokens(deep) > PROFILES["gemini"].long_context_threshold,
          str(estimate_tokens(deep)))

    lossless = ContextPlan(mode="lossless", default_mode="lossless")
    d1 = lossless.decide(deep, PROFILES["gemini"])
    check("lossless: never compacts", d1.action == "pass")
    check("lossless: reports the cliff", d1.crosses_cliff is True)
    check("lossless: budget raised",
          lossless.adjust_budget(24_000, PROFILES["gemini"]) >= 10_000_000)

    compact = ContextPlan(mode="compact", default_mode="compact")
    d2 = compact.decide(deep, PROFILES["gemini"])
    check("compact: compacts over cliff", d2.action == "compact", d2.reason)
    # compact 只会「收紧」预算，不会抬高：base 为 800k 字符（≈320k token，越过阶梯）时
    # 收到 170k 字符；base 本身已经在阶梯线下时保持原值。
    budget = compact.adjust_budget(800_000, PROFILES["gemini"])
    # 预算单位是**字符**，阶梯线是 **token**：必须换算，否则收窄量少算 2.5 倍
    # （在长会话里正好会把压缩顶到阶梯线以上、白白触发翻倍）。
    check("compact: budget clamped under cliff (chars vs tokens)",
          budget == int(200_000 * 0.85 * 2.5), str(budget))
    check("compact: never raises above base",
          compact.adjust_budget(24_000, PROFILES["gemini"]) == 24_000)

    d3 = compact.decide([{"role": "user", "content": "short"}], PROFILES["gemini"])
    check("compact: short context passes", d3.action == "pass")

    check("compact: no cliff -> base budget unchanged",
          compact.adjust_budget(24_000, PROFILES["deepseek"]) == 24_000)

    ask = ContextPlan(mode="ask", default_mode="compact")
    d4 = ask.decide(deep, PROFILES["gemini"], interactive=False)
    check("ask: non-interactive falls back to default", d4.mode == "compact")


# --------------------------------------------------------------------------- #
# cache-aware costing
# --------------------------------------------------------------------------- #

def test_pricing() -> None:
    print("\n-- 缓存感知计价 --")
    # DeepSeek flash：未命中 ¥1.08/M、输出 ¥4.32/M、命中 0.02×（低谷）
    parts = cost_of_usage("deepseek-flash", input_tokens=1_000_000, cached_tokens=900_000,
                          output_tokens=0, profile=PROFILES["deepseek"])
    expected = 100_000 * 1.08 / 1e6 + 900_000 * 1.08 * 0.02 / 1e6
    check("deepseek: 90% hit cost", abs(parts["total"] - round(expected, 6)) < 1e-6,
          f"got {parts['total']} want {round(expected, 6)}")
    check("deepseek: cached line present", parts["cached"] > 0)

    no_cache = cost_of_usage("deepseek-flash", input_tokens=1_000_000,
                             output_tokens=0, profile=PROFILES["deepseek"])
    # 90% 命中 + 0.02× 倍率 → 成本为全价的 0.1 + 0.9*0.02 = 11.8%，即省 88.2%。
    check("cache saves >85%", no_cache["total"] > parts["total"] * 8,
          f"{no_cache['total']} vs {parts['total']}")

    peak = cost_of_usage("deepseek-flash", input_tokens=1_000_000, output_tokens=0,
                         profile=PROFILES["deepseek"], peak=True)
    check("peak doubles", abs(peak["total"] - no_cache["total"] * 2) < 1e-6)

    batch = cost_of_usage("gpt-6.1-sol", input_tokens=1_000_000, output_tokens=0,
                          profile=PROFILES["openai"], batch=True)
    full = cost_of_usage("gpt-6.1-sol", input_tokens=1_000_000, output_tokens=0,
                         profile=PROFILES["openai"])
    check("batch halves", abs(batch["total"] - full["total"] * 0.5) < 1e-6)

    unknown = cost_of_usage("some-unknown-model", input_tokens=1_000_000, output_tokens=0)
    check("unknown model flagged estimated", unknown.get("estimated") == 1.0)

    # MiMo 的缓存口径 2026-10-07 已联网核实（命中 ¥0.025 vs 未命中 ¥3）。
    # 它不再属于「未公布」那一类，应拿到全车队最深的命中倍率，
    # 但仍然没有目录价 → 金额继续走混合价并标记 estimated。
    check("mimo: published deep multiplier",
          abs(PROFILES["mimo"].cache_read_multiplier - 0.025 / 3) < 1e-9,
          f"{PROFILES['mimo'].cache_read_multiplier:.6f}")
    check("mimo: deepest in the fleet",
          PROFILES["mimo"].cache_read_multiplier < PROFILES["deepseek"].cache_read_multiplier,
          f"mimo {PROFILES['mimo'].cache_read_multiplier:.4f} vs deepseek {PROFILES['deepseek'].cache_read_multiplier}")
    mimo = cost_of_usage("mimo-v2.6-flash", input_tokens=1_000_000, output_tokens=0,
                         profile=PROFILES["mimo"])
    check("mimo: still flagged estimated (no list price)", mimo.get("estimated") == 1.0)

    # 真正未知的那几家必须仍然保守：不给折扣、不判热。
    for vid in ("volcengine", "stepfun"):
        check(f"{vid}: stays conservative", PROFILES[vid].cache_read_multiplier == 1.0,
              PROFILES[vid].cache_mode)


def main() -> int:
    print("forge vendor-adaptation assertions")
    test_detection()
    test_usage()
    test_peak()
    test_shaping()
    test_fleet()
    test_context()
    test_pricing()
    total = len(_PASS) + len(_FAIL)
    print(f"\n{len(_PASS)}/{total} passed")
    if _FAIL:
        print("failed: " + ", ".join(_FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
