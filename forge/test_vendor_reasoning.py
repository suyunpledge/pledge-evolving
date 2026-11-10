"""Offline assertions for per-vendor reasoning params & usage shapes (2026-11-04).

Run directly::

    python -m forge.test_vendor_reasoning

Every assertion here is backed by a probe or a trace captured on this machine
on 2026-11-04 (vendor-probe-results.json / vendor-probe-mimo.json /
vendor-replay-results.json / workbuddy 产品配置与会话轨迹 / AutoClaw
model-token-usage.log). No network, no model calls.
"""

from __future__ import annotations

from .adapters import (
    HunyuanAdapter,
    MiMoAdapter,
    MiniMaxAdapter,
    VolcengineAdapter,
    adapter_for_id,
)
from .vendors import (
    PROFILES,
    VendorProfile,
    normalize_usage,
    profile_for,
    shape_reasoning,
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


def test_detection() -> None:
    print("\\n-- 厂商识别（本轮实证端点） --")
    check("url:hunyuan",
          profile_for(base_url="https://api.hunyuan.cloud.tencent.com/v1").id == "hunyuan")
    check("model:hy3 hint",
          profile_for(base_url="http://localhost:9999", model="hy3").id == "hunyuan")
    check("model:hy4-preview hint",
          profile_for(base_url="http://localhost:9999", model="hy4-preview").id == "hunyuan")
    check("url:qwen token-plan -> bailian",
          profile_for(base_url="https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1",
                      model="qwen3.8-max").id == "bailian")
    check("url:mimo token-plan",
          profile_for(base_url="https://token-plan-cn.xiaomimimo.com/v1",
                      model="mimo-v2.6-flash").id == "mimo")
    check("url:ark coding -> volcengine",
          profile_for(base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
                      model="doubao-seed-evolving").id == "volcengine")
    check("url:kimi coding -> kimi",
          profile_for(base_url="https://api.kimi.com/coding/v1",
                      model="k3.1-pro").id == "kimi")
    check("bailian(千问) enable_thinking 方言",
          PROFILES["bailian"].reasoning_style == "enable_thinking")
    flat = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36, "completion_tokens": 5}
    norm = normalize_usage(PROFILES["kimi"], flat)
    check("kimi 扁平命中字段（全局别名）",
          norm.get("cached_tokens") == 64 and norm.get("input_tokens") == 100)


def test_profile_fields() -> None:
    print("\\n-- 画像字段（探针实证） --")
    check("volcengine implicit",
          PROFILES["volcengine"].cache_mode == "implicit",
          PROFILES["volcengine"].cache_mode)
    check("volcengine thinking_type",
          PROFILES["volcengine"].reasoning_style == "thinking_type")
    check("deepseek thinking_type + 可关",
          PROFILES["deepseek"].reasoning_style == "thinking_type"
          and PROFILES["deepseek"].can_disable_thinking)
    check("glm efforts low/high/max",
          PROFILES["zhipu"].reasoning_efforts == ("low", "high", "max"),
          str(PROFILES["zhipu"].reasoning_efforts))
    check("minimax effort 分档",
          PROFILES["minimax"].reasoning_style == "effort"
          and PROFILES["minimax"].reasoning_efforts == ("low", "medium", "high"))
    check("mimo thinking_type",
          PROFILES["mimo"].reasoning_style == "thinking_type")
    check("hunyuan 不可关 + low/high",
          PROFILES["hunyuan"].reasoning_style == "effort"
          and PROFILES["hunyuan"].reasoning_efforts == ("low", "high")
          and not PROFILES["hunyuan"].can_disable_thinking)
    check("kimi effort 分档",
          PROFILES["kimi"].reasoning_style == "effort"
          and PROFILES["kimi"].reasoning_efforts == ("low", "high", "max"))


def test_normalize_usage() -> None:
    print("\\n-- usage 归一（真实返回体） --")
    # 混元轨迹：completion_tokens_details 在前且带恒 0 的 cached_tokens（防误读回归）
    hunyuan_usage = {
        "prompt_tokens": 33661, "completion_tokens": 191, "total_tokens": 33852,
        "completion_tokens_details": {
            "accepted_prediction_tokens": 0, "audio_tokens": 0, "reasoning_tokens": 138,
            "rejected_prediction_tokens": 0, "cached_tokens": 0},
        "prompt_tokens_details": {
            "accepted_prediction_tokens": 0, "audio_tokens": 0, "cached_tokens": 7872,
            "rejected_prediction_tokens": 0},
    }
    norm = normalize_usage(PROFILES["hunyuan"], hunyuan_usage)
    check("hunyuan prompt 侧命中不被 completion 侧恒 0 吃掉",
          norm.get("cached_tokens") == 7872, str(norm))
    check("hunyuan reasoning_tokens",
          norm.get("reasoning_tokens") == 138 and norm.get("input_tokens") == 33661)

    # 只有 completion 侧 cached_tokens（规格解码缓存）时不得计成命中
    spec_only = {"prompt_tokens": 10, "completion_tokens": 5,
                 "completion_tokens_details": {"cached_tokens": 5}}
    norm = normalize_usage(PROFILES["openai"], spec_only)
    check("规格解码 cached_tokens 不计命中", "cached_tokens" not in norm, str(norm))

    # 豆包探针 cache_A2：全命中
    doubao_usage = {"completion_tokens": 104, "prompt_tokens": 2378, "total_tokens": 2482,
                    "prompt_tokens_details": {"cached_tokens": 2378},
                    "completion_tokens_details": {"reasoning_tokens": 98}}
    norm = normalize_usage(PROFILES["volcengine"], doubao_usage)
    check("doubao 全命中 2378/2378",
          norm.get("cached_tokens") == 2378 and norm.get("input_tokens") == 2378)

    # mimo token-plan：prompt_tokens_details 为 null
    mimo_usage = {"completion_tokens": 22, "prompt_tokens": 18, "total_tokens": 40,
                  "completion_tokens_details": {"reasoning_tokens": 19},
                  "prompt_tokens_details": None}
    norm = normalize_usage(PROFILES["mimo"], mimo_usage)
    check("mimo null details 容错",
          norm.get("reasoning_tokens") == 19 and norm.get("input_tokens") == 18
          and norm.get("cached_tokens", 0) == 0, str(norm))

    # mimo 命中形态
    mimo_hit = {"prompt_tokens": 1862,
                "prompt_tokens_details": {"cached_tokens": 1856}}
    norm = normalize_usage(PROFILES["mimo"], mimo_hit)
    check("mimo 命中 1856", norm.get("cached_tokens") == 1856)

    # MiniMax：total_characters 忽略、details 解析
    mm_usage = {"total_tokens": 2399, "total_characters": 0, "prompt_tokens": 2391,
                "completion_tokens": 8,
                "completion_tokens_details": {"reasoning_tokens": 0},
                "prompt_tokens_details": {"cached_tokens": 128}}
    norm = normalize_usage(PROFILES["minimax"], mm_usage)
    check("minimax 命中/忽略 total_characters",
          norm.get("cached_tokens") == 128 and "total_characters" not in norm)

    # DeepSeek 双字段
    ds_usage = {"prompt_tokens": 2300, "completion_tokens": 28, "total_tokens": 2328,
                "prompt_tokens_details": {"cached_tokens": 2048},
                "completion_tokens_details": {"reasoning_tokens": 21},
                "prompt_cache_hit_tokens": 2048, "prompt_cache_miss_tokens": 252}
    norm = normalize_usage(PROFILES["deepseek"], ds_usage)
    check("deepseek hit/miss 并存",
          norm.get("cached_tokens") == 2048 and norm.get("input_tokens") == 2300)


def test_shape_reasoning() -> None:
    print("\\n-- reasoning 参数翻译 --")
    doubao = PROFILES["volcengine"]
    glm = PROFILES["zhipu"]
    minimax = PROFILES["minimax"]
    hunyuan = PROFILES["hunyuan"]
    generic = PROFILES["generic"]

    out, actions = shape_reasoning({"reasoning_effort": "high"}, doubao)
    check("doubao effort→摘参走 auto",
          "reasoning_effort" not in out and "thinking" not in out and bool(actions))

    out, _ = shape_reasoning({"thinking": {"type": "disabled"}}, doubao)
    check("doubao 显式关 → disabled", out.get("thinking") == {"type": "disabled"})

    out, _ = shape_reasoning({"thinking": {"type": "enabled"}}, doubao)
    check("doubao 显式开 → enabled", out.get("thinking") == {"type": "enabled"})

    out, _ = shape_reasoning({"enable_thinking": False}, glm)
    check("glm enable_thinking=False → thinking disabled",
          out.get("thinking") == {"type": "disabled"})

    out, actions = shape_reasoning({"thinking": {"type": "disabled"}}, minimax)
    check("minimax 关 → 家传 disabled",
          out.get("thinking") == {"type": "disabled"} and bool(actions))

    out, _ = shape_reasoning({"reasoning_effort": "medium"}, minimax)
    check("minimax medium 受支持原样保留", out.get("reasoning_effort") == "medium")

    out, actions = shape_reasoning({"thinking": {"type": "disabled"}}, hunyuan)
    check("hunyuan 关不掉 → 全摘 + 明说",
          not any(k in out for k in ("thinking", "reasoning_effort", "enable_thinking"))
          and any("不可关闭" in a for a in actions))

    out, actions = shape_reasoning({"reasoning_effort": "medium"}, hunyuan)
    check("hunyuan medium → 钳到 low",
          out.get("reasoning_effort") == "low"
          and any("钳到" in a for a in actions))

    out, _ = shape_reasoning({"reasoning_effort": "low"}, hunyuan)
    check("hunyuan low 原样", out.get("reasoning_effort") == "low")

    out, actions = shape_reasoning({"reasoning_effort": "high"}, generic)
    check("未知厂商不翻译",
          out == {"reasoning_effort": "high"} and actions == [])

    out, actions = shape_reasoning({"model": "x", "messages": []}, doubao)
    check("没带推理键就不动", out == {"model": "x", "messages": []} and actions == [])

    synth = VendorProfile(
        id="synth", label="合成", wire="openai", cache_mode="implicit",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0, min_cache_tokens=0,
        cache_ttl="managed", batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True, inject_cache_control=False,
        reasoning_style="enable_thinking")
    out, _ = shape_reasoning({"thinking": {"type": "enabled"}}, synth)
    check("enable_thinking 方言：开 → true", out.get("enable_thinking") is True)
    out, _ = shape_reasoning({"thinking": {"type": "disabled"}, "thinking_budget": 512}, synth)
    check("enable_thinking 方言：关 → false（budget 保留）",
          out.get("enable_thinking") is False and out.get("thinking_budget") == 512)

    a = shape_reasoning({"reasoning_effort": "high"}, minimax)
    b = shape_reasoning({"reasoning_effort": "high"}, minimax)
    check("翻译是纯函数（缓存字节稳定）", a == b)


def test_strip_think_and_output_floor() -> None:
    print("\n-- think 泄漏清洗与输出下限（2026-11-04 M3 真实任务实证） --")
    from .toolwire import strip_think_blocks

    check("闭合并置顶的 think 块被剥掉",
          strip_think_blocks("<think>推理……</think>\n\n答案是 4 个文件。") == "答案是 4 个文件。")
    check("未闭合（截断）从 think 起全丢",
          strip_think_blocks("<think>还没想完就被 max_tokens 截断") == "")
    check("正文中间的 think 不动",
          strip_think_blocks("代码示例：<think>tag</think> 保留") == "代码示例：<think>tag</think> 保留")
    check("无 think 原样", strip_think_blocks("普通回答") == "普通回答")
    check("空串安全", strip_think_blocks("") == "")
    check("minimax 输出下限 4096", PROFILES["minimax"].min_output_tokens == 4096)
    # 第三轮 e2e 后扩展：四家思考型厂商全部有逼近 2048 上限的实证。
    check("思考型厂商输出下限 4096（证据族）",
          all(PROFILES[v].min_output_tokens == 4096
              for v in ("minimax", "deepseek", "volcengine", "mimo", "hunyuan")))
    check("无思考上限压力证据的厂商默认无下限",
          PROFILES["anthropic"].min_output_tokens == 0
          and PROFILES["bailian"].min_output_tokens == 0)


def test_round3_vendor_budgets() -> None:
    print("\n-- 第三轮：按厂家压缩地板 / 步数提示 / 轨迹投影 --")
    from types import SimpleNamespace

    from .context_plan import ContextPlan
    from .protocol import run_payload

    for vid in ("deepseek", "volcengine", "mimo", "minimax", "zhipu", "hunyuan"):
        check(f"{vid} 上下文地板 96000", PROFILES[vid].context_floor_chars == 96_000,
              str(PROFILES[vid].context_floor_chars))
    for vid in ("anthropic", "openai", "gemini", "kimi", "bailian", "stepfun"):
        check(f"{vid} 无地板（保持默认）", PROFILES[vid].context_floor_chars == 0)
    check("M3 steps_hint=24（实测撞顶两回）", profile_for(model="minimax-m3").steps_hint == 24)
    check("MiniMax 未识别型号不继承 M3 调优", PROFILES["minimax"].steps_hint == 0)
    check("其余 steps_hint=0",
          PROFILES["deepseek"].steps_hint == 0 and PROFILES["mimo"].steps_hint == 0
          and PROFILES["volcengine"].steps_hint == 0)

    plan = ContextPlan(mode="ask", default_mode="compact")
    check("adjust_budget 抬到地板（mimo）",
          plan.adjust_budget(24_000, PROFILES["mimo"], mode="compact") == 96_000)
    check("地板不压低显式大预算",
          plan.adjust_budget(200_000, PROFILES["minimax"], mode="compact") == 200_000)
    check("lossless 不受地板影响",
          plan.adjust_budget(24_000, PROFILES["mimo"], mode="lossless") >= 10_000_000)

    rep = SimpleNamespace(
        text="ok", stopped="final",
        usage={"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 60,
               "cache_write_tokens": 0, "reasoning_tokens": 0,
               "requests": [
                   {"usage": {"prompt_tokens": 100, "cached_tokens": 0}},
                   {"usage": {"prompt_tokens": 120, "cached_tokens": 110}},
                   {"usage": {"prompt_tokens": 90, "cached_tokens": 5}},
                   {"usage": {"prompt_tokens": 130, "cached_tokens": 120}},
               ]},
        tool_calls=0, planning=None, run_id="r", task_id="t", parent_run_id="",
        steps=[])
    rep.as_result = lambda: SimpleNamespace(outcome="succeeded", error_code="",
                                            execution_state="completed")
    payload = run_payload(rep)
    traj = payload["usage"].get("cache_trajectory") or {}
    check("轨迹：请求数", traj.get("requests") == 4)
    check("轨迹：均值命中", abs(traj.get("avg_hit_rate", 0) - 0.4738) < 0.01,
          str(traj.get("avg_hit_rate")))
    check("轨迹：断点定位", traj.get("busts") == [2], str(traj.get("busts")))
    check("轨迹：冷启动标记", traj.get("cold_start") is True)


def test_adapters() -> None:
    print("\\n-- 专属 adapter 注册 --")
    check("mimo adapter", isinstance(adapter_for_id("mimo"), MiMoAdapter))
    check("minimax adapter", isinstance(adapter_for_id("minimax"), MiniMaxAdapter))
    check("volcengine adapter", isinstance(adapter_for_id("volcengine"), VolcengineAdapter))
    check("hunyuan adapter", isinstance(adapter_for_id("hunyuan"), HunyuanAdapter))


def main() -> int:
    test_detection()
    test_profile_fields()
    test_normalize_usage()
    test_shape_reasoning()
    test_strip_think_and_output_floor()
    test_round3_vendor_budgets()
    test_adapters()
    total = len(_PASS) + len(_FAIL)
    print(f"\n{len(_PASS)}/{total} passed")
    if _FAIL:
        print("failed:", ", ".join(_FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
