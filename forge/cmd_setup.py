"""
pledge-evolving 首次运行向导 — 交互式配置模型接入

用法：python run.py setup

向导流程（v0.9 起）：

  1. 接入模式：单厂家（缓存共享、账单一行）还是模型混排（每档挑最划算的）
  2. 厂商与型号：单厂家选一家 + 档位；混排为每个档位分别选厂商
  3. 长上下文编排：无损输入 / 压缩输入 / 每次询问
  4. API 密钥

第 1、2 步决定了每个 provider 的 ``vendor`` 字段——框架据此选择专属请求方式
（是否注入 cache_control、命中价倍率、峰谷判定），而不是对所有厂商发同一种请求。
第 3 步决定 ``context.policy``，由 loop 在越过长上下文阶梯前执行。
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from .context_plan import CONTEXT_MODES
from .vendors import PROFILES, VENDOR_ORDER

HOME = Path.home() / ".forge"
USER_LAYER = HOME / "forge.patch.json"

# --------------------------------------------------------------------------- #
# 厂商目录：接入所需的最小字段 + 每个档位的推荐型号
# --------------------------------------------------------------------------- #

VENDOR_CATALOG: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "wire": "openai",
        "env_key": "FORGE_DEEPSEEK_KEY",
        "placeholder": "sk-...",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "tiers": {"lite": "deepseek-flash", "medium": "deepseek-flash", "premium": "deepseek-v4-pro"},
        "hint": "缓存命中价全市场最低（未命中的 2%），且有峰谷五折；无长上下文阶梯",
    },
    "anthropic": {
        "label": "Anthropic（Claude）",
        "base_url": "https://api.anthropic.com",
        "wire": "anthropic",
        "env_key": "FORGE_ANTHROPIC_KEY",
        "placeholder": "sk-ant-...",
        "models": ["claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"],
        "tiers": {"lite": "claude-haiku-4-5", "medium": "claude-sonnet-5-5", "premium": "claude-opus-5-5"},
        "hint": "缓存必须显式打标记（本向导已自动开启）；命中 0.10×，Opus 5.5 为 0.05×",
    },
    "openai": {
        "label": "OpenAI（GPT 系列）",
        "base_url": "https://api.openai.com/v1",
        "wire": "openai",
        "env_key": "FORGE_OPENAI_KEY",
        "placeholder": "sk-...",
        "models": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
        "tiers": {"lite": "gpt-6-luna", "medium": "gpt-6.1-sol", "premium": "gpt-6-astra"},
        "hint": "自动缓存；有 short/long 两档上下文价（超阈值翻倍），另有 Flex 五折档",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "wire": "openai",
        "env_key": "FORGE_GEMINI_KEY",
        "placeholder": "AIza...",
        "models": ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro"],
        "tiers": {"lite": "gemini-3.5-flash-lite", "medium": "gemini-3.8-flash", "premium": "gemini-3.1-pro"},
        "hint": "隐式缓存自动开启（最小前缀 4096）；超 200K 输入输出同时翻倍",
    },
    "bailian": {
        "label": "阿里云百炼（Qwen）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "wire": "openai",
        "env_key": "FORGE_DASHSCOPE_KEY",
        "placeholder": "sk-...",
        "models": ["qwen3.8-flash", "qwen3.8-max"],
        "tiers": {"lite": "qwen3.8-flash", "medium": "qwen3.8-flash", "premium": "qwen3.8-max"},
        "hint": "显式（0.10×）与隐式（0.20×）缓存互斥；工具定义逐字节稳定才能命中",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "wire": "openai",
        "env_key": "FORGE_ZHIPU_KEY",
        "placeholder": "鉴权密钥",
        "models": ["glm-5.3-flash", "glm-5.3"],
        "tiers": {"lite": "glm-5.3-flash", "medium": "glm-5.3-flash", "premium": "glm-5.3"},
        "hint": "隐式缓存自动识别（最小前缀 512），命中 25%；思考分 low/high/max 三档",
    },
    "kimi": {
        "label": "Kimi 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "wire": "openai",
        "env_key": "FORGE_MOONSHOT_KEY",
        "placeholder": "sk-...",
        "models": ["kimi-k2.6", "kimi-k3"],
        "tiers": {"lite": "kimi-k2.6", "medium": "kimi-k2.6", "premium": "kimi-k3"},
        "hint": "自动缓存（5min / 1h 两档 TTL）；k3 的缓存写入单独计费",
    },
    "mimo": {
        "label": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "wire": "openai",
        "env_key": "FORGE_MIMO_KEY",
        "placeholder": "sk-...",
        "models": ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"],
        "tiers": {"lite": "mimo-v2.6-flash", "medium": "mimo-v2.6-flash", "premium": "mimo-v2.6-pro"},
        "hint": "全车队有效单价最低（约 ¥0.0754/M）；官方未公布缓存口径，按原价保守估算",
    },
    "minimax": {
        "label": "MiniMax",
        "base_url": "https://api.minimaxi.com/v1",
        "wire": "openai",
        "env_key": "FORGE_MINIMAX_KEY",
        "placeholder": "sk-...",
        "models": ["minimax-m2.7-highspeed", "minimax-m3"],
        "tiers": {"lite": "minimax-m2.7-highspeed", "medium": "minimax-m2.7-highspeed", "premium": "minimax-m3"},
        "hint": "隐式缓存（命中约 10–20%，最小前缀 512）",
    },
}

TIER_LABELS = {
    "lite": "经济档 lite（简单任务、杂务）",
    "medium": "均衡档 medium（主力）",
    "premium": "高端档 premium（集成裁决 / 复杂任务）",
}

# Keep the previous single-provider setup API available to integrations.
PROVIDERS = [{"id": vid, "name": entry["label"], "model": entry["tiers"]["medium"], **entry}
             for vid, entry in VENDOR_CATALOG.items()]
PROVIDERS += [{"id": service, "name": service, "service": service, "wire": "openai",
               "base_url": "http://127.0.0.1:11434" if service == "ollama" else "http://127.0.0.1:8080/v1",
               "model": "qwen3:8b" if service == "ollama" else ""}
              for service in ("ollama", "llamacpp", "mnn")]
PROVIDERS.append({"id": "custom", "name": "自定义", "wire": "openai", "base_url": "", "model": ""})


def _build_patch(provider: dict, api_key: str, custom_url: str = "", custom_model: str = "") -> list[dict]:
    """Compatibility entry for callers configuring one real provider."""
    model = custom_model or provider["model"]
    config = {"wire": provider["wire"], "baseURL": custom_url or provider["base_url"],
              "apiKey": api_key, "model": model, "smallModel": model}
    if provider.get("service"):
        config["service"] = provider["service"]
    return [{"id": "medium", "name": f"provider:{provider['id']}", "config": config},
            {"id": "model", "name": "model:router", "config": {
                "primary": ["medium", model], "fallback": [], "moa": False, "moaModels": [],
                "routing": {"strategy": "medium", "tiers": [["medium", model]],
                            "small": ["medium", model]}}}]


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _banner():
    print("""
╔══════════════════════════════════════════════════╗
║       pledge-evolving · 首次运行向导            ║
║                                                  ║
║  三步：接入模式 → 厂商型号 → 上下文编排         ║
╚══════════════════════════════════════════════════╝
""")


def _ask_choice(prompt: str, options: list[tuple[str, str]], default: str = "1") -> str:
    """打印带编号的选项并返回所选 value。"""
    for i, (_, label) in enumerate(options, 1):
        print(f"  [{i}] {label}")
    print()
    while True:
        raw = input(f"{prompt}（默认 {default}）> ").strip() or default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        # 也允许直接填 value（方便脚本化 / 老手）
        if raw in {value for value, _ in options}:
            return raw
        print(f"无效输入，请输入 1-{len(options)}")


# --------------------------------------------------------------------------- #
# 第一步：接入模式
# --------------------------------------------------------------------------- #

def _choose_mode() -> str:
    print("\n第一步 · 接入模式\n")
    print("  单厂家模式：所有档位都用同一个厂商的模型。")
    print("    ✓ 厂商适配规则统一；缓存仍按端点、模型和账户隔离，升级不保证命中")
    print("    ✓ 账单只有一行，密钥只有一个，前缀策略只有一套")
    print("    ✗ 每档都要接受这家厂商的性价比")
    print()
    print("  模型混排模式：经济 / 均衡 / 高端三档分别挑不同厂商。")
    print("    ✓ 每档都能选当时最划算的模型，成本曲线更优")
    print("    ✗ 厂商之间缓存互相隔离——每次升级跳跃都要冷启动、按全价重算前缀")
    print("    ✗ 要管多把密钥、多套计费口径")
    print()
    return _ask_choice("选择接入模式", [("single", "单厂家模式"), ("mixed", "模型混排模式")])


def _choose_vendor(title: str, *, default: str = "deepseek") -> str:
    ordered = [v for v in VENDOR_ORDER if v in VENDOR_CATALOG]
    options = []
    for vid in ordered:
        entry = VENDOR_CATALOG[vid]
        profile = PROFILES.get(vid)
        tail = f" — {entry['hint']}" if entry.get("hint") else ""
        options.append((vid, f"{entry['label']}{tail}"))
    default_index = str(ordered.index(default) + 1) if default in ordered else "1"
    print(f"\n{title}\n")
    return _ask_choice("选择厂商", options, default=default_index)


def _choose_model(vendor: str, tier: str) -> str:
    entry = VENDOR_CATALOG[vendor]
    models = entry["models"]
    suggested = entry["tiers"].get(tier, models[0])
    if len(models) == 1:
        return models[0]
    default_index = str(models.index(suggested) + 1) if suggested in models else "1"
    print(f"\n  {entry['label']} — {TIER_LABELS[tier]}")
    options = [(m, m + ("（推荐）" if m == suggested else "")) for m in models]
    return _ask_choice("  选择型号", options, default=default_index)


# --------------------------------------------------------------------------- #
# 第二步：组装档位 -> (vendor, model)
# --------------------------------------------------------------------------- #

def _plan_single() -> dict[str, tuple[str, str]]:
    vendor = _choose_vendor("第二步 · 选择厂商（单厂家模式：三档同厂）")
    entry = VENDOR_CATALOG[vendor]

    print(f"\n{entry['label']} 的档位安排：")
    for tier in ("lite", "medium", "premium"):
        print(f"  {TIER_LABELS[tier]:<32} → {entry['tiers'][tier]}")
    print()
    keep = _ask_choice("沿用这套档位安排", [("y", "是"), ("n", "否，我逐档指定")], default="1")
    if keep == "y":
        return {tier: (vendor, entry["tiers"][tier]) for tier in ("lite", "medium", "premium")}

    plan: dict[str, tuple[str, str]] = {}
    for tier in ("lite", "medium", "premium"):
        plan[tier] = (vendor, _choose_model(vendor, tier))
    return plan


def _plan_mixed() -> dict[str, tuple[str, str]]:
    print("\n第二步 · 逐档选择厂商（模型混排模式）")
    print("  提示：厂商之间缓存隔离，档位跨度越大，升级跳跃的全价重算越多。\n")
    plan: dict[str, tuple[str, str]] = {}
    for tier in ("lite", "medium", "premium"):
        vendor = _choose_vendor(f"{TIER_LABELS[tier]}", default={"lite": "zhipu", "medium": "deepseek",
                                                                 "premium": "anthropic"}[tier])
        plan[tier] = (vendor, _choose_model(vendor, tier))
    return plan


# --------------------------------------------------------------------------- #
# 第三步：长上下文编排
# --------------------------------------------------------------------------- #

def _choose_context_policy(plan: dict[str, tuple[str, str]]) -> str:
    cliffs = []
    for tier, (vendor, _) in plan.items():
        profile = PROFILES.get(vendor)
        if profile and profile.long_context_threshold:
            cliffs.append(f"{profile.label}（{profile.long_context_threshold:,} token 以上单价 ×{profile.long_context_multiplier:g}）")

    print("\n第三步 · 长上下文编排\n")
    if cliffs:
        print("  你的档位里有厂商是按「阶梯」计费的：")
        for row in cliffs:
            print(f"    · {row}")
        print("  也就是说，上下文多写一个 token 越过阶梯线，整次调用单价翻倍。")
    else:
        print("  你的档位里没有阶梯计费的厂商——上下文按量线性计费，多送只是多付 token。")
        print("  但压缩仍能省输入 token，代价是可能丢失早期细节。")
    print()
    print("  [1] 每次都问我 —— 越线时暂停并给出「省钱 / 保信息」两个选项")
    print("  [2] 总是无损   —— 上下文完整保留，接受更高成本")
    print("  [3] 总是压缩   —— 自动压缩早期内容，优先省钱")
    print()
    return _ask_choice("选择策略",
                       [("ask", "每次都问我"), ("lossless", "总是无损"), ("compact", "总是压缩")],
                       default="1")


def _choose_default_for_ask() -> str:
    print("\n  无人值守时（cron / 无头运行）无法询问，此时默认走哪一边？")
    return _ask_choice("  默认策略", [("compact", "压缩（省钱）"), ("lossless", "无损（保信息）")],
                       default="1")


# --------------------------------------------------------------------------- #
# 第四步：密钥
# --------------------------------------------------------------------------- #

def _collect_keys(plan: dict[str, tuple[str, str]]) -> dict[str, str]:
    vendors = list(dict.fromkeys(v for v, _ in plan.values()))
    keys: dict[str, str] = {}
    print(f"\n第四步 · API 密钥（{len(vendors)} 个厂商）\n")
    for vid in vendors:
        entry = VENDOR_CATALOG[vid]
        env_key = entry["env_key"]
        existing = os.environ.get(env_key, "")
        if existing:
            masked = existing[:6] + "..." + existing[-4:] if len(existing) > 10 else "***"
            print(f"检测到环境变量 {env_key} = {masked}")
            use = input("使用这个密钥？(Y/n) > ").strip().lower()
            if use != "n":
                keys[vid] = existing
                continue
        print(f"\n请输入 {entry['label']} 的 API 密钥：")
        print("  （保存到 ~/.forge/forge.patch.json，不会上传）")
        keys[vid] = getpass.getpass(f"  {env_key} > ").strip()
    return keys


# --------------------------------------------------------------------------- #
# 组装 patch
# --------------------------------------------------------------------------- #

def _tier_of(vendor: str, model: str) -> str:
    """给一个 (vendor, model) 找一个默认档位角色。"""
    entry = VENDOR_CATALOG.get(vendor)
    if not entry:
        return "medium"
    for tier, suggested in entry["tiers"].items():
        if suggested == model:
            return tier
    models = entry["models"]
    if model == models[0]:
        return "lite"
    if model == models[-1] and len(models) > 1:
        return "premium"
    return "medium"


def build_patch(
    plan: dict[str, tuple[str, str]],
    keys: dict[str, str],
    contexts: dict[str, str | bool],
) -> list[dict]:
    """把向导结果变成 forge.patch.json 的行。

    每个 provider 行显式写 ``vendor``——不依赖 URL 猜测，用户换聚合网关时
    适配仍然是钉死的。``cacheControl`` 只在厂商需要显式标记时才写。
    """
    used_vendors = list(dict.fromkeys(v for v, _ in plan.values()))
    # 同一厂商在多个档位出现时，provider 行 id 加后缀去重（medium / medium-2）。
    row_ids: dict[tuple[str, str], str] = {}
    seen: dict[str, int] = {}
    for tier in ("lite", "medium", "premium"):
        vendor, model = plan[tier]
        base = tier
        seen[base] = seen.get(base, 0) + 1
        row_ids[(tier, model)] = base if seen[base] == 1 else f"{base}-{seen[base]}"

    patch: list[dict] = []
    tier_rows: list[list[str]] = []

    for tier in ("lite", "medium", "premium"):
        vendor, model = plan[tier]
        entry = VENDOR_CATALOG[vendor]
        profile = PROFILES.get(vendor)
        row_id = row_ids[(tier, model)]
        config: dict = {
            "wire": entry["wire"],
            "baseURL": entry["base_url"],
            # Values collected in this process must survive its exit. The env
            # expression remains available for callers that explicitly omit keys.
            "apiKey": keys[vendor] if vendor in keys else {"$expr": f"get('env.{entry['env_key']}', '')"},
            "model": model,
            "smallModel": model,
            "vendor": vendor,
        }
        # 只有需要显式标记的厂商才开 cacheControl，其余保持自动缓存。
        if profile is not None and profile.explicit_cache:
            config["cacheControl"] = "explicit"
        patch.append({"id": row_id, "name": f"provider:{vendor}", "config": config})
        tier_rows.append([row_id, model])

    primary = tier_rows[1]  # medium
    patch.append({
        "id": "model",
        "name": "model:router",
        "config": {
            "primary": primary,
            "fallback": [],
            "moa": False,
            "moaModels": [],
            "routing": {
                "strategy": "medium",
                "tiers": [primary],
                "premium": tier_rows[2:],
                "small": tier_rows[0],
                "notes": (
                    "由 forge setup 生成的档位：lite=经济 / medium=均衡 / premium=高端集成。"
                    "每个 provider 行都钉了 vendor 字段，框架据此选择专属请求方式。"
                ),
            },
        },
    })

    patch.append({
        "id": "context",
        "name": "context:policy",
        "config": {
            "mode": contexts.get("mode", "ask"),
            "defaultMode": contexts.get("default_mode", "compact"),
            "keepTail": 6,
            "notes": (
                "长上下文编排：lossless=完整上下文（成本高）/ compact=越线前压缩（省钱）/ "
                "ask=交互时询问、无人值守回落到 defaultMode。"
            ),
        },
    })

    if len(used_vendors) > 1:
        print("\n[提醒] 混排模式下各厂商缓存互相隔离——"
              "从均衡档升级到高端档时，前缀会在新厂商处冷启动并按全价重算一次。")

    return patch


def _save(patch: list[dict]):
    HOME.mkdir(parents=True, exist_ok=True)
    existing = []
    if USER_LAYER.is_file():
        try:
            existing = json.loads(USER_LAYER.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []
    seen = {r["id"] for r in patch}
    merged = patch + [r for r in existing if r.get("id") not in seen]
    USER_LAYER.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 配置已保存到 {USER_LAYER}")


def _verify():
    print("\n验证配置...")
    try:
        from .config import load_config
        from .model import ModelRouter

        pkg_bundles = Path(__file__).resolve().parent / "bundles"
        repo_bundles = Path(__file__).resolve().parent.parent / "bundles"
        bundle_dir = pkg_bundles if pkg_bundles.is_dir() else repo_bundles

        cfg = load_config(HOME, bundles=sorted(bundle_dir.glob("*.json")))
        router = ModelRouter.from_config(cfg)
        fleet = router.fleet()

        print(f"✅ 发现 {len(router.providers)} 个 provider"
              + (f"（厂商：{', '.join(fleet['vendors'])}）" if fleet["vendors"] else ""))
        print(f"✅ 接入模式：{'单厂家' if fleet['mode'] == 'single' else '模型混排'}"
              + "，缓存按端点、模型与账户隔离，命中以实际 usage 为准")
        if fleet["any_explicit_cache"]:
            print("✅ 已为需要显式标记的厂商开启 cacheControl")
        print("✅ 配置验证通过！")
    except Exception as e:
        print(f"⚠️  验证出错（不影响使用）: {e}")


def _show_next_steps():
    print("""
╔══════════════════════════════════════════════════╗
║                 🎉 配置完成！                   ║
╠══════════════════════════════════════════════════╣
║  python run.py run "帮我写一首诗"                ║
║  python run.py run "..." --context-policy ask    ║
║  python run.py vendors          # 查看厂商适配   ║
║  python run.py cost rate        # 对比单价       ║
║  python run.py doctor / selftest                 ║
╚══════════════════════════════════════════════════╝
""")


def cmd_setup(args) -> int:
    _clear()
    _banner()

    if USER_LAYER.is_file():
        try:
            existing = json.loads(USER_LAYER.read_text(encoding="utf-8"))
            provider_rows = [r for r in existing if str(r.get("id", "")).startswith(("lite", "medium", "premium"))]
            if provider_rows:
                print(f"检测到已有配置（{len(provider_rows)} 个 provider）")
                if input("重新配置？(y/N) > ").strip().lower() != "y":
                    print("保持现有配置，直接使用。")
                    _verify()
                    return 0
        except (json.JSONDecodeError, OSError):
            pass

    mode = _choose_mode()
    plan = _plan_single() if mode == "single" else _plan_mixed()

    contexts: dict[str, str | bool] = {"mode": _choose_context_policy(plan)}
    if contexts["mode"] == "ask":
        contexts["default_mode"] = _choose_default_for_ask()
    else:
        contexts["default_mode"] = contexts["mode"]

    keys = _collect_keys(plan)
    missing = [v for v, k in keys.items() if not k]
    if missing:
        print(f"❌ 未输入密钥：{', '.join(missing)}，配置取消。")
        return 1

    patch = build_patch(plan, keys, contexts)
    _save(patch)
    _verify()
    _show_next_steps()
    return 0
