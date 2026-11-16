"""Vendor adaptation layer — one request shape per billing model.

Why this module exists
----------------------
Seven vendors, seven billing shapes. The same ``messages`` list sent to two
providers produces two different invoices, and a few of the differences are
not cosmetic — they change what you are allowed to send:

* **Cache control is a request field on some vendors and a no-op on others.**
  Anthropic needs ``cache_control`` on the block you want cached; DeepSeek,
  OpenAI, Gemini and GLM cache automatically and ignore the marker. Sending
  the marker to a vendor that does not know the field is a 400 risk, so the
  injection is gated per profile instead of being global.

* **Reasoning tokens are billed as output.** Gemini says so outright; the
  thinking modes of DeepSeek and GLM behave the same way. A budget decision
  therefore has to know whether "more thinking" costs input rates or output
  rates — a 4–5x difference.

* **Peak/valley pricing exists on exactly one vendor.** DeepSeek halves every
  rate off-peak. Nothing in the request changes; only *when* it is sent. The
  scheduler has to be able to ask "is this an expensive window right now".

* **Long context is a cliff, not a slope.** OpenAI and Gemini double the rate
  once the prompt crosses a threshold. Crossing it to preserve 100 tokens of
  history is a bad trade, and the decision needs to be visible to the caller.

This module is deliberately data-first: ``PROFILES`` is a table, detection is
a function over that table, and every other module asks the table rather than
hard-coding a vendor name.

Usage::

    from .vendors import profile_for, shape_anthropic_cache, normalize_usage

    profile = profile_for(base_url="https://api.deepseek.com", model="deepseek-flash")
    profile.peak_valley        # True
    profile.cache_read_multiplier  # 0.02
"""

from __future__ import annotations

import math
import hashlib
import json
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Any, Iterable
from .model_kinds import manufacturer_for_model, resolve_model_kind

# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VendorProfile:
    """Everything the framework needs to know about one vendor's billing shape."""

    id: str
    label: str
    wire: str                      # preferred wire format: "openai" | "anthropic"
    # -- cache ---------------------------------------------------------------
    cache_mode: str                # implicit | explicit_anthropic | explicit_openai | none
    cache_read_multiplier: float   # hit price / base input price
    cache_write_multiplier: float  # write price / base input price (1.0 = not charged)
    min_cache_tokens: int          # prefix shorter than this is never cached
    cache_ttl: str                 # human label: "5m" | "30m" | "1h" | "disk" | "managed"
    # -- modifiers -----------------------------------------------------------
    batch_discount: float          # multiplier when using the batch API (0.5 = half)
    supports_flex: bool            # cheaper-but-slower service tier available
    peak_valley: bool              # time-of-day pricing
    # -- long context --------------------------------------------------------
    long_context_threshold: int    # 0 = no cliff
    long_context_multiplier: float  # applied once the prompt crosses the threshold
    # -- reasoning -----------------------------------------------------------
    reasoning_billed_as_output: bool
    # -- request shaping -----------------------------------------------------
    inject_cache_control: bool     # default policy for putting the marker on the wire
    # -- reasoning（2026-11-04 分厂家探针实证）--------------------------------
    # 请求侧推理控制的方言。"" = 不翻译（透传，维持未知端点的现状）：
    #   thinking_type  → thinking={"type": "enabled"|"disabled"}（豆包/GLM/DeepSeek/MiMo）
    #   effort         → reasoning_effort 分档（MiniMax/混元/Kimi/GPT/Step）
    #   enable_thinking→ enable_thinking 布尔（千问 DashScope 风格）
    reasoning_style: str = ""
    # 实测支持的 effort 档位（有序低→高）。空 = 未核实，不做档位钳制。
    reasoning_efforts: tuple[str, ...] = ()
    # Some thinking.type endpoints also accept an effort parameter. Keep the
    # verified combination instead of erasing an explicit caller choice.
    supports_reasoning_effort: bool = False
    # 思考能否整体关闭。以探针为准；workbuddy 的 canDisableThinking 作旁证。
    can_disable_thinking: bool = True
    # 默认输出预算（token）；用户显式 max_tokens/think_budget 优先。
    # 2026-11-04 实测：MiniMax M3 的思考走 content（<think> 块），forge 主循环
    # 默认 2048 被思考整个吃掉（reasoning 2047/2048）——finish=length 截断，
    # 答案根本没生成。4096 = 思考(~2k)+答案(~2k) 的实测安全线。
    min_output_tokens: int = 0
    # 压缩天花板的地板（字符）。24k 默认是小窗口时代遗产；1M 窗口+实证高命中的
    # 厂商在 24k 就压缩=把整段前缀打回全价（2026-11-04 e2e：MiniMax 一轮 5 次
    # 压缩 5 个断点，71.2% 均命中的大头损失；deepseek 不压缩跑到 95.4%）。
    # 只对无阶梯线（slope）厂商生效，cliff 厂商仍以阶梯封顶优先。0=保持默认。
    context_floor_chars: int = 0
    steps_hint: int = 0
    # ---- 分级检索（长期记忆）模型适配（2026-11-04 第四轮）----
    # 记忆块走请求尾部（每轮全价、不可缓存），预算=每轮尾成本+噪声面；
    # 门槛=客户端精度闸（服务端已按 THRESH_KEY 过滤关键句，此处再按厂家
    # 噪声敏感度收紧）；top_k=检索宽度（宽检索+紧门槛=高精度高召回）。
    # 证据：第三轮 e2e 命中/上下文经济学 + M3 验证型啰嗦习惯。
    memory_char_budget: int = 1500
    memory_min_score: float = 0.0
    memory_top_k: int = 0    # 0=服务端默认
    # Provider row IDs and gateway brands are not model identities.
    model_kind_id: str = ""
    model_family_id: str = ""
    manufacturer_id: str = ""
    canonical_model_id: str = ""
    model_match_source: str = ""
    model_evidence: str = ""
    model_traits: tuple[str, ...] = ()
    behavior_fingerprint: str = ""
    # -- telemetry -----------------------------------------------------------
    usage_aliases: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @property
    def explicit_cache(self) -> bool:
        return self.cache_mode.startswith("explicit")

    def to_raw(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "wire": self.wire,
            "cache_mode": self.cache_mode,
            "cache_read_multiplier": self.cache_read_multiplier,
            "cache_write_multiplier": self.cache_write_multiplier,
            "min_cache_tokens": self.min_cache_tokens,
            "cache_ttl": self.cache_ttl,
            "batch_discount": self.batch_discount,
            "supports_flex": self.supports_flex,
            "peak_valley": self.peak_valley,
            "long_context_threshold": self.long_context_threshold,
            "long_context_multiplier": self.long_context_multiplier,
            "reasoning_billed_as_output": self.reasoning_billed_as_output,
            "inject_cache_control": self.inject_cache_control,
            "reasoning_style": self.reasoning_style,
            "reasoning_efforts": list(self.reasoning_efforts),
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "can_disable_thinking": self.can_disable_thinking,
            "min_output_tokens": self.min_output_tokens,
            "context_floor_chars": self.context_floor_chars,
            "steps_hint": self.steps_hint,
            "memory_char_budget": self.memory_char_budget,
            "memory_min_score": self.memory_min_score,
            "memory_top_k": self.memory_top_k,
            "model_kind_id": self.model_kind_id,
            "model_family_id": self.model_family_id,
            "manufacturer_id": self.manufacturer_id or self.id,
            "canonical_model_id": self.canonical_model_id,
            "model_match_source": self.model_match_source,
            "model_evidence": self.model_evidence,
            "model_traits": list(self.model_traits),
            "behavior_fingerprint": self.behavior_fingerprint,
        }


# Canonical usage keys every vendor is normalised onto.
CANONICAL_USAGE_KEYS = (
    "input_tokens",         # total prompt-side tokens (cached + uncached)
    "cached_tokens",        # subset of input that was a cache read
    "cache_write_tokens",   # tokens written to cache on this request
    "output_tokens",        # generated tokens
    "reasoning_tokens",     # subset of output spent on thinking
)

# Shared alias table: raw field name -> canonical key. Vendor rows extend this.
_BASE_USAGE_ALIASES: dict[str, str] = {
    # OpenAI-shaped
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cached_tokens": "cached_tokens",
    "cache_write_tokens": "cache_write_tokens",
    # Anthropic-shaped
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    # DeepSeek/Kimi 家族的扁平命中字段（2026-11-04：kimi coding 与部分中转同形态）。
    # 放基表而不是 deepseek 专属：这是字段名映射不是折扣假设，多认一家无副作用。
    "prompt_cache_hit_tokens": "cached_tokens",
    "prompt_cache_miss_tokens": "_uncached_tokens",
    # OpenAI details 嵌套的精确路径（pass-1 精确命中，优先于叶名兜底）。
    # 2026-11-04 防误读：混元 / DeepSeek v4.1 轨迹实测 completion_tokens_details
    # 里也带 cached_tokens（恒 0，规格解码缓存），且在 JSON 里排 prompt 侧之前——
    # 叶名兜底会先吃到它而漏掉 prompt 侧真命中。此处用精确路径钉死 prompt 侧。
    "prompt_tokens_details.cached_tokens": "cached_tokens",
    "input_tokens_details.cached_tokens": "cached_tokens",
    "completion_tokens_details.reasoning_tokens": "reasoning_tokens",
    "output_tokens_details.reasoning_tokens": "reasoning_tokens",
    # reasoning
    "reasoning_tokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "thoughts_token_count": "reasoning_tokens",
}


# --------------------------------------------------------------------------- #
# the table
# --------------------------------------------------------------------------- #

PROFILES: dict[str, VendorProfile] = {
    # ---- DeepSeek: the cheapest cache in the market, plus time-of-day pricing
    "deepseek": VendorProfile(
        id="deepseek", label="DeepSeek", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.02, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="disk",
        batch_discount=1.0, supports_flex=False, peak_valley=True,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,   # automatic; a marker would be ignored noise
        reasoning_style="thinking_type", reasoning_efforts=("low", "high", "max"),
        supports_reasoning_effort=True,
        can_disable_thinking=True,
        context_floor_chars=96_000,
        # 2026-11-04 第三轮 e2e：末请求 completion=2048/reasoning=2048，输出预算
        # 全被 reasoning_content 吃掉、content 未起笔→空文本终答。同族四家均现上限压力。
        min_output_tokens=4096,
        memory_char_budget=2000, memory_top_k=8,
        usage_aliases={
            "prompt_cache_hit_tokens": "cached_tokens",
            "prompt_cache_miss_tokens": "_uncached_tokens",
        },
        notes="硬盘缓存默认开启，命中价为未命中的 2%（flash）/3.3%（v4-pro）。"
              "高峰为 UTC 周一至周五 01–04、06–10，其余时段全部五折。",
    ),

    # ---- Anthropic: the only vendor where the marker is mandatory
    "anthropic": VendorProfile(
        id="anthropic", label="Anthropic", wire="anthropic",
        cache_mode="explicit_anthropic",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="5m",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=True,
        usage_aliases={},
        notes="必须显式打 cache_control 标记才会缓存；5 分钟档写入 1.25×、"
              "命中 0.10×；1 小时档写入 2×。Opus 5.5 命中 0.05×、Fable 5.1 命中 0.025×。"
              "2026-11-04 Claude Code 生产轨迹（3 会话 1143 请求去重）："
              "Opus 5.5 100% 用 ephemeral_1h 档（零次 5m），命中率 95-98%，"
              "思考占输出 20-30%，input_tokens 恒为 2-4，基座约 52,200 token。",
    ),

    # ---- OpenAI: automatic, with an optional explicit mode on newer models
    "openai": VendorProfile(
        id="openai", label="OpenAI", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="30m",
        batch_discount=0.5, supports_flex=True, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=2.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        # One local Codex setting proves high was used, not the complete set
        # supported by every OpenAI model. Preserve caller effort unchanged.
        reasoning_style="effort", reasoning_efforts=(),
        can_disable_thinking=True,
        notes="默认自动缓存（读取 0.1×，GPT-6.1 Sol 为 0.05×）。"
              "short/long 两档上下文价；Flex 档约 5 折，Fast 档 2×、Ultrafast 6×。"
              "2026-11-04：本机 Codex config.toml 实证 model_reasoning_effort=high，"
              "该配置不能证明所有模型的接口档位枚举，显式 effort 保留原值。"
              "同日 gpt-6-astra 生产轨迹：reasoning_effort=xhigh（新档位），"
              "字段用 input_tokens 而非 prompt_tokens（归一层已兼容）。",
    ),

    # ---- Gemini: implicit only, and explicit caching lives on another API
    "gemini": VendorProfile(
        id="gemini", label="Google Gemini", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.0,
        min_cache_tokens=4096, cache_ttl="managed",
        batch_discount=0.5, supports_flex=True, peak_valley=False,
        long_context_threshold=200_000, long_context_multiplier=2.0,
        memory_char_budget=1000, memory_min_score=0.25,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={"total_cached_tokens": "cached_tokens",
                       "cachedContentTokenCount": "cached_tokens"},
        notes="隐式缓存默认开启，最小前缀 4096（3.x）/2048（2.5）。"
              "超 200K 输入输出同时翻倍；显式缓存另收存储费（按 1M·小时）。"
              "2026-11-04：Antigravity agentapi 无头实测，flash 档内部模型 id 为 "
              "gemini-3.8-flash-tiered（tiered=分级计费档），模型提示词命中 gemini "
              "即可识别本画像。",
    ),

    # ---- Alibaba Bailian: both modes, explicit needs a marker on messages
    "bailian": VendorProfile(
        id="bailian", label="阿里云百炼", wire="openai",
        cache_mode="explicit_bailian",
        cache_read_multiplier=0.10, cache_write_multiplier=1.25,
        min_cache_tokens=1024, cache_ttl="5m",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,   # opt-in: explicit and implicit are mutually exclusive
        # 2026-11-04：千问 OpenAI 兼容面的思考开关是 enable_thinking（DashScope 文档）；
        # 本机 token-plan key 403 Unpurchased 无法直连实证，档位表留空不钳制。
        reasoning_style="enable_thinking",
        can_disable_thinking=True,
        usage_aliases={"prompt_tokens_details.cached_tokens": "cached_tokens",
                       "input_tokens_details.cached_tokens": "cached_tokens"},
        notes="显式缓存（标记 1.25× 写、0.10× 读）与隐式缓存（0.20× 读）互斥，"
              "单请求只能二选一。工具定义参与缓存，顺序与字段必须逐字节一致。",
    ),

    # ---- Zhipu GLM: implicit, cheapest among the domestic automatic providers
    "zhipu": VendorProfile(
        id="zhipu", label="智谱 GLM", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.25, cache_write_multiplier=1.0,
        min_cache_tokens=512, cache_ttl="managed",
        batch_discount=0.5, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        reasoning_style="thinking_type", reasoning_efforts=("low", "high", "max"),
        can_disable_thinking=True,
        context_floor_chars=96_000,
        memory_char_budget=2000, memory_top_k=8,
        notes="隐式缓存自动识别，最小前缀 512。GLM-5.3 命中 25%。"
              "思考强度分 low/high/max 三档，思考 token 按输出价计费。"
              "2026-11-04 探针：coding 端点 thinking{enabled/disabled} 干净生效"
              "（关闭时 reasoning_tokens=0）；AutoClaw 真机 4611 次调用轨迹命中 "
              "95.8%/96.8%、cacheWrite 恒 0。ZCode 实证另有 anthropic 兼容线 "
              "open.bigmodel.cn/api/anthropic（cache_control ephemeral + "
              "thinking{type:enabled} 直发可用），anthropic wire 接入时可用。",
    ),

    # ---- Kimi: explicit, and the only one that bills cache storage by time
    "kimi": VendorProfile(
        id="kimi", label="Kimi 月之暗面", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.10, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="5m",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        usage_aliases={},
        reasoning_style="effort", reasoning_efforts=("low", "high", "max"),
        can_disable_thinking=True,
        notes="重复前缀自动缓存，分 5min / 1h 两档 TTL。k3 缓存写入单独计费"
              "（¥20 / 1M，1h 档 ¥40）。2026-11-04 本机 kimi-code config.toml 实证："
              "coding 端点 api.kimi.com/coding/v1，k3/k3.1-pro/k2.8 全系列 "
              "support_efforts=[low,high,max]（k2.8 默认 max、k3 默认 high）、"
              "always_thinking 能力。",
    ),

    # ---- MiMo (Xiaomi): deepest cache discount in the fleet, on paper
    # 2026-10-07 联网核实：MiMo-V2.5-Pro 官方口径为输入（命中缓存）¥0.025/M、
    # 输入（未命中）¥3/M、输出 ¥6/M → 命中倍率 0.025/3 ≈ 0.83%，
    # 比 DeepSeek 的 2% 还低。多家媒体（腾讯新闻/搜狐/cnblogs，9/22–9/27）
    # 报道 MiMo-V2.6 系列沿用 V2.5 定价，故此处按同一口径填入。
    #
    # 注意与 EFFECTIVE_RATES 的区别：pricing.EFFECTIVE_RATES 里 mimo 的
    # ¥0.0754/M 是从账单反推的**有效混合单价**，其中含 Token Plan 订阅积分的
    # 折算，远低于目录价。两者回答不同问题——此处给倍率（尺度无关），
    # 价格仍走 EFFECTIVE_RATES，于是决策对、金额也贴合实际付费。
    "mimo": VendorProfile(
        id="mimo", label="小米 MiMo", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.025 / 3.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        reasoning_style="thinking_type",
        can_disable_thinking=True,
        context_floor_chars=96_000,
        min_output_tokens=4096,
        memory_char_budget=2000, memory_top_k=8,
        notes="命中倍率 0.83%（¥0.025 vs 未命中 ¥3），全车队最低，比 DeepSeek 的 2% 还深。"
              "官方未公布最小前缀与 TTL；本地温暖度估计不是服务端保留承诺。"
              "另有低谷优惠：00:00–08:00（UTC+8）按 0.8× 计。"
              "2026-11-04 token-plan 探针：隐式缓存实证（同题重发 1856/1862 命中、"
              "换尾题 1024 部分命中）；usage 为 OpenAI details 风格，无缓存时"
              "prompt_tokens_details 为 null（缺失回执保持未知）；thinking.type=on/off 干净生效"
              "（关闭时 reasoning_tokens=0），enable_thinking 亦被接受。"
              "2026-11-09 轨迹画像（6 类任务两轮实测）：代码类高效直达（生成/排障/重构"
              " 6-13 步、真实工具错误率 0）；聚合统计类低效——倾向逐条 grep/artifact_read"
              " 翻页后在回复里逐数列式，两种死法：churn 烧穿步数（30 步 max_steps）或"
              " 长推导爆输出限（4096 仍不够）；对聚合任务应在任务提示或导航提醒里"
              "引导 shell_exec 运行 python 一步算完。2026-11-09 R2 复验：提示级干预双重无效——任务文本显式引导 shell 与窗口化 nudge（滑动窗口 8 中同工具≥6 实战首触发）双双生效后，模型仍 read_range×12 翻页、在回复里逐行求和至输出爆限；聚合低效是模型级顽固倾向，下一步应走工具层（聚合工具或工具结果内嵌提示），非提示层。2026-11-09 R3 工具层验证：新增 file_stats 聚合工具（key=value 解析+等值过滤+sum/count/avg/min/max，支持逗号分隔多文件）后，同一 E 任务三跑 3/3 正确完成、全部干净收尾（final）且零输出爆限（R1/R2 为 0/3）——致命模式消灭；步数 12/13/22 方差大（≤8 未稳定达成，模型仍混入探索/验证步），核心计算已收敛为 file_stats×N+calculator+write_file。工具层是聚合类低效的正确解法。2026-11-09 G-K 轮（Work/Coding 模式 5 新类型）：G 测试编写/H 调试运行循环/I 多文件协同一次全过（模型会用 shell_exec 自测迭代）；J 文档写作自发迁移 file_stats×7 生成分组表（工具跨任务迁移实证）；K 数据转换三败三诊——模型试图在回复里逐行写 360 行 CSV（output_limit），给 file_export 导出工具后 360 行秒出，再修解析器方括号头映射（[module_0001]→module 字段）后全列正确通过。要点：MiMo 对'输出大量结构化行'的任务必须给落盘/导出工具，不能指望它用 write_file 手写或 shell 脚本自觉。2026-11-09 M-P 轮（摘要/Git/审查/性能优化）：M 摘要✓（22 步，先用 shell_exec 失败后转 grep 统计+write_file 完成）、O 审查✓（5 步高效：read→write 一气呵成）、P 优化✓（edit_file 直改+外部验证；首轮 output_limit 死于长解释回复——加'回答只需两三句'后 9 步通过）、**N Git✗ 根因=forge 层**：受保护 agent 配置下 shell_exec 整体禁用（'Native execution unavailable in protected agent'），MiMo 每次尝试都失败但不识别阻断，反复重试+tool_search/skill_list/spawn_subagent 游荡烧穿17-14 步——需要命令行的任务（git/构建/测试运行）在受保护配置下对任何模型都不可完成，应提供 mediated git 工具或明确禁用提示（一次性说明而非每次报错）。模型行为画像补充：受挫时不放弃（反复换路径尝试）是优点也是步数杀手。2026-11-09 R4：新增 mediated git 四件套（git_status/git_add/git_commit/git_log，白名单子命令+stdlib subprocess+30s 超时，受保护配置可用），N 类从 0%→**7 步 88s 通过**（git_status→git_add→git_commit→git_log→verify 完美序列）——给出正确工具后 MiMo 即刻走最短路径，模型潜力受限于工具面而非能力。缓存：工具密集负载 90.0%、"
              "纯累积稳态 98.6-99.5%，跨实例续接依赖会话前缀快照持久化。",
    ),

    # ---- MiniMax: implicit cache, discount varies by model generation
    "minimax": VendorProfile(
        id="minimax", label="MiniMax", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=0.20, cache_write_multiplier=1.0,
        min_cache_tokens=512, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        reasoning_style="effort", reasoning_efforts=("low", "medium", "high"),
        can_disable_thinking=True,
        min_output_tokens=4096,
        context_floor_chars=96_000,
        # M3's observed loop/memory tuning belongs to its model-kind card;
        # an unknown MiniMax generation keeps the ordinary defaults.
        notes="隐式缓存，命中倍率随代际浮动（M3 / M2.7 约 20%，M2.5 / M2.1 约 10%，"
              "取自阿里云百炼托管口径）。这里取 20% 偏保守，避免高估节省。"
              "2026-11-04 M3 探针：reasoning_effort low/medium/high 分档生效"
              "（思考 token 58/100/175）；thinking.type 仅认 adaptive/disabled"
              "（enabled/auto/off 均 400）；命中呈部分/非单调计入。",
    ),

    # ---- Volcengine / Doubao
    # 2026-10-07 联网核实：火山方舟确实有「缓存命中」计费项——
    # doubao-seed-evolving 的在线推理（常规）缓存命中单价已启用（第三方监测站
    # 2026-09-22 记录到该行存在）；豆包 2.1 Pro 的已公布比率为命中 ¥1.2 vs
    # 未命中 ¥6 = 20%。但 **seed-evolving 自己的比率与最小前缀未公开**，
    # 所以这里仍不填具体倍率：把 2.1 Pro 的 20% 套到另一个模型上就是编造。
    "volcengine": VendorProfile(
        id="volcengine", label="火山方舟（豆包）", wire="openai",
        # 2026-11-04 ark coding 端点探针：隐式前缀缓存实证——同题重发
        # cached_tokens 2378/2378 全命中，换尾题 1848/2379 部分命中（块粒度）。
        # 折扣率官方仍未公布（豆包 2.1 Pro 公布 20% 但不能套用），倍率按原价
        # 保守计——命中会被 usage 如实报出，cost_of_usage 不会夸大节省。
        cache_mode="implicit",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        reasoning_style="thinking_type",
        can_disable_thinking=True,
        context_floor_chars=96_000,
        min_output_tokens=4096,
        memory_char_budget=2000, memory_top_k=8,
        notes="隐式缓存已实证（2026-11-04 探针：同题重发 2378/2378、换尾题 "
              "1848/2379，usage 报 prompt_tokens_details.cached_tokens）；折扣率"
              "未公布，金额按原价保守估算。thinking.type=on/off 干净生效（关闭时 "
              "reasoning_tokens=0）；返回带 encrypted_content，原样回放实测 200。"
              "缓存写入滞后一拍（e2e 八连请求：cached 恒等于上上轮 prompt，"
              "两次才追平），短间隔连发时命中率天然偏低约 10-15pt，非前缀纪律问题。",
    ),

    # ---- StepFun
    "stepfun": VendorProfile(
        id="stepfun", label="阶跃星辰 StepFun", wire="openai",
        cache_mode="unknown",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="unknown",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        reasoning_style="effort", reasoning_efforts=("low", "medium", "high"),
        can_disable_thinking=False,
        notes="百炼托管的 step 系列在隐式缓存名单内，但未公布折扣比例，按原价估算。",
    ),

    # ---- Tencent Hunyuan（混元）：WorkBuddy(CodeBuddy) 产品配置 + 轨迹实证
    # 2026-11-04 证据：wb-product-config.json 60 模型注册表（hy3：onlyReasoning、
    # effort low/high 默认 high、canDisableThinking=false、temperature 0.9/top_p 1、
    # 192K 上下文；hy4-preview：仅 high、960K）；workbuddy 会话 jsonl 轨迹（hy3
    # cached_tokens 7872 命中、deepseek-v4.1-flash 38656/38931）。缓存折扣未核实，
    # 倍率按原价保守。usage 注意：completion_tokens_details 也带恒 0 的 cached_tokens
    #（归一层已按 prompt 侧精确路径优先防误读）。
    "hunyuan": VendorProfile(
        id="hunyuan", label="腾讯混元", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=True,
        inject_cache_control=False,
        reasoning_style="effort",
        reasoning_efforts=("low", "high"),
        can_disable_thinking=False,
        context_floor_chars=96_000,
        min_output_tokens=4096,
        memory_char_budget=2000, memory_top_k=8,
        notes="hy3/hy4 为 onlyReasoning（思考不可关）；hy3 有 low/high，hy4 仅 high。"
              "WorkBuddy 的 hy4free / hy4-free 路由已实测 credit=0，勿外推为所有混元 API 免费。"
              "WorkBuddy 产品配置采样 temperature=0.9/top_p=1。"
              "隐式缓存已有轨迹实证（cached 7872），折扣未核实按原价保守。",
    ),

    # ---- local engines: no billing, but the same interface
    "local": VendorProfile(
        id="local", label="本地推理引擎", wire="openai",
        cache_mode="none",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=0, cache_ttl="none",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=False,
        inject_cache_control=False,
        notes="本地引擎不按 token 计费，缓存折扣不适用。适配重点是多轮工具调用的"
              "历史重放（走 OpenAI 兼容端点）。",
    ),

    # ---- fallback: behave like a plain OpenAI-compatible endpoint
    "generic": VendorProfile(
        id="generic", label="未识别端点", wire="openai",
        cache_mode="implicit",
        cache_read_multiplier=1.0, cache_write_multiplier=1.0,
        min_cache_tokens=1024, cache_ttl="managed",
        batch_discount=1.0, supports_flex=False, peak_valley=False,
        long_context_threshold=0, long_context_multiplier=1.0,
        reasoning_billed_as_output=False,
        inject_cache_control=False,
        notes="未识别的端点：保守处理，不注入任何厂商专属字段，按原价估算。",
    ),
}

# Vendor ids in the order the setup wizard offers them.
VENDOR_ORDER: tuple[str, ...] = (
    "deepseek", "anthropic", "openai", "gemini", "bailian", "zhipu", "kimi",
    "mimo", "minimax", "volcengine", "stepfun", "hunyuan", "local",
)

# Substrings that identify a vendor from a base URL.
# 2026-10-07: 改成「主机名 + 域名后缀」匹配，不再对整条 URL 做子串匹配。
# 旧写法有两个真问题：
#   ① https://api.openai.com.attacker.example/v1 会被当成 OpenAI（子串命中）
#   ② https://other.example/api.openai.com/v1 也会被当成 OpenAI（路径里带域名）
# 域名归属必须看 host，不能看 path，也不能看 host 的后缀区。
_VENDOR_DOMAINS: dict[str, tuple[str, ...]] = {
    "deepseek": ("api.deepseek.com", "deepseek.com"),
    "anthropic": ("api.anthropic.com", "anthropic.com"),
    "openai": ("api.openai.com", "openai.com", "openai.azure.com"),
    "gemini": ("generativelanguage.googleapis.com", "ai.google.dev", "googleapis.com"),
    "bailian": ("dashscope.aliyuncs.com", "aliyuncs.com", "bailian.console.aliyun.com",
                 "qianwenaiapi.com"),   # 千问 token-plan: token-plan.maas.qianwenaiapi.com
    "zhipu": ("open.bigmodel.cn", "bigmodel.cn", "zhipu.ai"),
    "kimi": ("api.moonshot.cn", "moonshot.cn", "moonshot.ai", "kimi.com"),   # 2026-11-04：kimi-code config.toml 实证 coding 端点 api.kimi.com/coding/v1
    "mimo": ("api.xiaomimimo.com", "xiaomimimo.com", "mimo.xiaomi.com"),
    "minimax": ("api.minimaxi.com", "minimaxi.com", "minimax.chat", "minimax.io"),
    "volcengine": ("ark.cn-beijing.volces.com", "volces.com", "volcengine.com"),
    "stepfun": ("api.stepfun.com", "stepfun.com", "stepfun.ai"),
    "hunyuan": ("hunyuan.cloud.tencent.com", "hunyuan.tencent.com", "hunyuan.io"),
}

# 环境标识：环回/内网地址不可能属于任何云厂商。
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")


def _host_of(url: str) -> str:
    """提取纯主机名（小写，去掉端口与用户信息）。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw if "//" in raw else "//" + raw)
    return (parsed.hostname or "").lower()


def _host_matches(host: str, domain: str) -> bool:
    """host 等于该域名，或者是它的真子域（点边界），防止后缀欺骗。"""
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)

_LOCAL_SERVICES = {"ollama", "llamacpp", "mnn"}


def profile_for(
    *, base_url: str = "", model: str = "", service: str = "",
    vendor: str = "", wire: str = "", model_kind_id: str = "",
) -> VendorProfile:
    """Resolve the actual model kind before manufacturer/endpoint fallbacks.

    A local engine retains its local transport/billing policy. Otherwise a
    known model kind wins over a reseller's name, vendor tag or host. Opaque
    deployment names can use an explicit model_kind_id. The wire model is
    never rewritten; account/endpoint/model cache isolation remains separate.
    """
    kind, matched = resolve_model_kind(model, model_kind_id)

    def selected(vid: str, source: str) -> VendorProfile:
        profile = PROFILES[vid]
        if kind and vid != "local":
            profile = replace(profile, **dict(kind.overrides))
        profile = replace(profile,
            model_kind_id=kind.id if kind else "",
            model_family_id=kind.family_id if kind else "",
            manufacturer_id=kind.manufacturer_id if kind else vid,
            canonical_model_id=kind.canonical_model if kind else "",
            model_match_source=source,
            model_evidence=("local_unverified" if vid == "local" else
                            ("family_inherited:" + kind.evidence if matched == "family_prefix" else
                             kind.evidence) if kind else "inherited_unverified"),
            model_traits=kind.traits if kind and vid != "local" else ())
        # Fingerprint the effective capability contract, not account secrets,
        # provider labels, response system_fingerprint or a sample's prose.
        fields = ("id", "model_kind_id", "model_family_id", "manufacturer_id", "reasoning_style",
                  "reasoning_efforts", "supports_reasoning_effort", "can_disable_thinking",
                  "min_output_tokens", "context_floor_chars", "steps_hint", "memory_char_budget",
                  "memory_min_score", "memory_top_k", "cache_mode", "model_traits",
                  "wire", "inject_cache_control", "min_cache_tokens", "cache_ttl",
                  "long_context_threshold", "long_context_multiplier")
        signature = json.dumps({k: getattr(profile, k) for k in fields},
                               sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return replace(profile, behavior_fingerprint=hashlib.sha256(signature.encode()).hexdigest()[:16])

    if service and service.lower() in _LOCAL_SERVICES:
        return selected("local", "local_service")
    if kind:
        return selected(kind.manufacturer_id, matched)
    manufacturer = manufacturer_for_model(model)
    if manufacturer:
        return selected(manufacturer, "manufacturer_prefix")
    if vendor and vendor in PROFILES:
        return selected(vendor, "vendor_fallback")

    host = _host_of(base_url)
    if host and not any(host == h for h in _LOCAL_HOSTS):
        for vid, domains in _VENDOR_DOMAINS.items():
            if any(_host_matches(host, domain) for domain in domains):
                return selected(vid, "endpoint_fallback")
    return selected("generic", "unknown")


# --------------------------------------------------------------------------- #
# peak / valley
# --------------------------------------------------------------------------- #

# DeepSeek: peak = UTC Mon–Fri 01:00–04:00 and 06:00–10:00.
# Chinese public holidays are off-peak in full; we cannot compute the holiday
# calendar offline, so we expose the flag and let the caller pass a holiday set.
_DEEPSEEK_PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_peak(profile: VendorProfile, when: float | None = None,
            *, holiday: bool = False) -> bool:
    """True when ``when`` (epoch seconds) falls in a vendor's expensive window."""
    if not profile.peak_valley:
        return False
    if holiday:
        return False
    stamp = time.gmtime(when if when is not None else time.time())
    if stamp.tm_wday >= 5:            # Saturday / Sunday
        return False
    hour = stamp.tm_hour
    return any(start <= hour < end for start, end in _DEEPSEEK_PEAK_WINDOWS_UTC)


def price_multiplier(profile: VendorProfile, *, when: float | None = None,
                     holiday: bool = False, batch: bool = False,
                     flex: bool = False, prompt_tokens: int = 0) -> float:
    """Combined multiplier from the three *usage-shape* dimensions.

    Deliberately excludes the cache multipliers: those apply per token class
    rather than to the whole request, and mixing them here would make the
    result meaningless. This is the "same request, different circumstances"
    factor, which is the one a scheduler can actually act on.
    """
    factor = 1.0
    if is_peak(profile, when, holiday=holiday):
        factor *= 2.0
    if batch and profile.batch_discount < 1.0:
        factor *= profile.batch_discount
    if flex and profile.supports_flex:
        factor *= 0.5
    if profile.long_context_threshold and prompt_tokens > profile.long_context_threshold:
        factor *= profile.long_context_multiplier
    return factor


# --------------------------------------------------------------------------- #
# usage normalisation
# --------------------------------------------------------------------------- #


def _flatten(node: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten a nested usage object into dotted keys (bounded depth)."""
    out: dict[str, Any] = {}
    if depth > 3:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            out[dotted] = value
            if isinstance(value, dict):
                out.update(_flatten(value, dotted, depth + 1))
    return out


def _usable_count(value: Any) -> int | None:
    """有限非负整数，否则 None。

    Usage 字段是外部输入（上游响应），可能是 inf / nan / 负数：
    int(inf) 会抛 OverflowError，int(nan) 抛 ValueError，负数会把
    命中率与成本算成负的。统一在这里丢成 0，而不是让一次脏响应
    把整条请求炸掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0:
        return None
    return int(value)


def normalize_usage(profile: VendorProfile, raw: dict[str, Any] | None) -> dict[str, int]:
    """Map a vendor's ``usage`` object onto the canonical key set.

    Two passes: an exact (dotted) match first, then a leaf-name fallback so
    nested shapes work without every vendor needing a hand-written path.
    Precise prompt-side paths override generic leaf names regardless of JSON
    key order. Output-side cached tokens never count as prompt cache hits.
    Unknown fields are ignored; malformed values
    (infinite / NaN / negative) are dropped rather than propagated.
    """
    aliases = {**_BASE_USAGE_ALIASES, **profile.usage_aliases}
    flat = _flatten(raw or {})
    out: dict[str, int] = {}

    # Pass 1 — exact dotted matches (most specific wins).
    for key in sorted(flat, key=lambda name: (name.count("."), name)):
        value = flat[key]
        canonical = aliases.get(key)
        if canonical:
            count = _usable_count(value)
            if count is not None:
                out[canonical] = count

    # Pass 2 — leaf-name fallback for anything still unresolved.
    for key, value in flat.items():
        leaf = key.rsplit(".", 1)[-1]
        if leaf == "cached_tokens" and any(part in {"completion_tokens_details", "output_tokens_details"}
                                           for part in key.split(".")):
            continue  # 规格解码缓存（恒 0），不是 prompt 前缀缓存命中
        canonical = aliases.get(leaf)
        if canonical and canonical not in out:
            count = _usable_count(value)
            if count is not None:
                out[canonical] = count

    # DeepSeek reports hit and miss separately; total input is the sum.
    if "_uncached_tokens" in out:
        miss = out.pop("_uncached_tokens")
        hits = out.get("cached_tokens", 0)
        if "input_tokens" not in out:
            out["input_tokens"] = miss + hits

    return out


def hit_rate(usage: dict[str, int]) -> float:
    """Fraction of prompt tokens served from cache (0.0–1.0)."""
    total = int(usage.get("input_tokens", 0))
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, int(usage.get("cached_tokens", 0)) / total))


# --------------------------------------------------------------------------- #
# request shaping
# --------------------------------------------------------------------------- #


def should_inject_cache(profile: VendorProfile, requested: str = "") -> bool:
    """Decide whether to put a cache marker on this request.

    ``requested`` comes from provider config: ``"auto"`` (profile decides),
    ``"off"`` (never), ``"explicit"`` (force on).
    """
    mode = (requested or "auto").strip().lower()
    if mode in ("off", "none", "false", "0"):
        return False
    if mode in ("explicit", "on", "true", "1"):
        return True
    return profile.inject_cache_control


def shape_anthropic(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    ttl: str = "",
    max_breakpoints: int = 4,
) -> dict[str, Any]:
    """Attach ``cache_control`` to the stable prefix of an Anthropic request.

    Three breakpoints are used, one per cacheable *layer* rather than one per
    block: tools first (they change the least), then the system prompt, then
    the rolling edge of the conversation history. That matches the vendor's
    own hierarchy (``tools`` → ``system`` → ``messages``) so a change to the
    system prompt does not invalidate the tools entry.

    The marker is placed on the **last** element of each layer — the marker
    means "cache everything up to and including this block", so putting it on
    the last tool, the last system block and the last content block of the
    last message covers all three layers in full. The history breakpoint is
    what makes an agent loop affordable on this wire: turn k+1 reads turn
    k's write at the hit price for the entire shared prefix, and the write
    premium only ever covers the new tail (2026-11-01 — the history layer
    previously never carried a marker, so every turn re-paid full input
    price for the whole conversation).

    2026-10-07 修正：去掉“工具数超过 32 就不打标记”的旧上限。那一行会让
    工具很多的 agent（真实场景常见）彻底拿不到工具层缓存，而断点数量与
    工具个数无关——永远只需最后一个。

    Returns the payload, mutated in place for convenience.
    """
    marker: dict[str, Any] = {"type": "ephemeral"}
    if ttl in ("1h", "3600"):
        marker["ttl"] = "1h"

    # -- tools ---------------------------------------------------------------
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        last = tools[-1]
        if isinstance(last, dict):
            last["cache_control"] = dict(marker)

    # -- system --------------------------------------------------------------
    system = payload.get("system")
    if isinstance(system, str) and system.strip():
        payload["system"] = [{"type": "text", "text": system,
                              "cache_control": dict(marker)}]
    elif isinstance(system, list) and system:
        last = system[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = dict(marker)

    # -- messages ------------------------------------------------------------
    # 2026-11-01：会话历史是 agent 循环里最大的可缓存资产，此前却从不承载
    # 断点——这一线厂商每轮都要为整段历史付全价。滚动标记打在最后一条
    # 消息的最后一个内容块上：写入点覆盖到对话末尾，下一轮请求对上一轮
    # 写入的整段共享前缀按命中价读取，只有新增尾部按写入价计费。上方
    # tools / system 两个断点在尾部被改写或重生成时仍保住头部命中。
    #
    # 滚动必须先摘旧再打新：上一轮的写入点标记若不摘除，第 3 轮起断点
    # 总数就会超过该厂商 4 个的上限，请求被 400 拒绝。标记本身是注解、
    # 不参与缓存前缀键，摘旧标记不会使已写入的缓存失效。
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        last_message = messages[-1]
        if isinstance(last_message, dict):
            content = last_message.get("content")
            if isinstance(content, str) and content.strip():
                last_message["content"] = [{"type": "text", "text": content,
                                            "cache_control": dict(marker)}]
            elif isinstance(content, list) and content:
                block = content[-1]
                if isinstance(block, dict):
                    block["cache_control"] = dict(marker)

    return payload


def shape_bailian(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    max_breakpoints: int = 4,
) -> dict[str, Any]:
    """Attach a Bailian-style explicit marker to the last **stable** block.

    2026-10-07 修正：旧实现打在最后一条消息上，那是每轮都变的用户输入——
    结果每次都写一份新缓存、永远命中不了上一次，比不开缓存还贵。
    稳定块应该是系统消息（system 角色），它跨请求不变、且工具定义会作为
    系统消息的一部分参与缓存。没有系统消息时不动（宁可不缓存，不误伤）。

    Bailian（DashScope）在 OpenAI 兼容面上接受把 Anthropic 风格的
    ``cache_control`` 放在 message 的 content 块里；工具定义不能单独挂标记。
    本行为是 opt-in（profile 的 ``inject_cache_control`` 为 False），
    因为显式与隐式缓存在该家互斥，而隐式模式本来就默认开着且免费。
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload

    target = None
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            target = message
    if target is None:
        return payload

    marker = {"type": "ephemeral"}
    content = target.get("content")
    if isinstance(content, str):
        if not content.strip():
            return payload
        target["content"] = [{"type": "text", "text": content, "cache_control": dict(marker)}]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = dict(marker)
    return payload


def shape_request(
    payload: dict[str, Any],
    profile: VendorProfile,
    *,
    cache_control: str = "auto",
    ttl: str = "",
) -> dict[str, Any]:
    """Apply the vendor's request adaptation. No-op when the marker is off."""
    if not should_inject_cache(profile, cache_control):
        return payload
    if profile.cache_mode == "explicit_anthropic":
        return shape_anthropic(payload, profile, ttl=ttl)
    if profile.cache_mode == "explicit_bailian":
        return shape_bailian(payload, profile)
    return payload


# --------------------------------------------------------------------------- #
# reasoning 参数翻译（2026-11-04 分厂家探针实证）
# --------------------------------------------------------------------------- #

_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")


def _clamp_effort(level: str, supported: tuple[str, ...]) -> str:
    """把请求档位钳到厂商支持档位：取不超过请求档位的最高支持档。

    请求 medium 而厂商只有 low/high → low（宁可少想，不擅自加码）；
    请求 low 而厂商最低是 high → high（没法更低就按厂商下限）。
    """
    if not supported or level not in _EFFORT_ORDER:
        return level
    req = _EFFORT_ORDER.index(level)
    candidates = [s for s in supported
                  if s in _EFFORT_ORDER and _EFFORT_ORDER.index(s) <= req]
    return candidates[-1] if candidates else supported[0]


def _thinking_off(thinking: Any, enable_thinking: Any) -> bool:
    if isinstance(thinking, dict) and str(thinking.get("type", "")).lower() in (
            "disabled", "off", "none", "false"):
        return True
    return enable_thinking is False


def shape_reasoning(payload: dict[str, Any],
                    profile: VendorProfile) -> tuple[dict[str, Any], list[str]]:
    """把调用方的推理控制翻译成该厂商认的方言，摘掉它不认的键。

    输入键（model.HttpTransport 白名单透传）：reasoning_effort / thinking /
    thinking_budget / enable_thinking。翻译表全部来自 2026-11-04 实测：

    * ``effort`` 厂商（MiniMax/混元/Kimi/GPT/Step）：保留 reasoning_effort
      （按 reasoning_efforts 钳档）；关闭请求——MiniMax 用它家独有的
      thinking={"type":"disabled"}（allowed=adaptive/disabled，探针 400 实证），
      其余 effort 厂商缺少已验证关闭方言时，摘参走默认并说明未关闭；
      不可关（混元 onlyReasoning）则忽略并明说。
    * ``thinking_type`` 厂商（豆包/GLM/DeepSeek/MiMo）：显式 on/off 翻译成
      thinking={"type":"enabled"|"disabled"}；effort 档位语义不支持——探针
      实证这几家强制 enabled 会把每轮思考 token 从 0 抬到数百，档位请求
      未核实档位的厂家摘参走默认 auto；DeepSeek 的直连 high 已验证，保留显式 effort。
    * ``enable_thinking`` 厂商（千问 DashScope 风格，尚无直连实证）：布尔翻译；
      thinking_budget 与 enable_thinking=True 同行有效，保留。
    * 未知厂商（style=""）：不翻译不摘除——维持透传，不替未知端点做主。

    返回 (payload, actions)。actions 进运行报告（plan.actions），解释
    "这条请求的思考参数为什么长这样"。翻译是纯函数：同 payload+profile
    恒同输出，不破坏前缀缓存的字节稳定性。
    """
    style = profile.reasoning_style
    keys = ("reasoning_effort", "thinking", "thinking_budget", "enable_thinking")
    if not style or not any(key in payload for key in keys):
        return payload, []

    out = dict(payload)
    actions: list[str] = []
    thinking = out.get("thinking")
    enable = out.get("enable_thinking")
    effort = out.get("reasoning_effort")

    if style == "effort":
        out.pop("thinking", None)
        out.pop("enable_thinking", None)
        budget = out.pop("thinking_budget", None)
        off = _thinking_off(thinking, enable)
        if off and not profile.can_disable_thinking:
            out.pop("reasoning_effort", None)
            actions.append(f"{profile.label} 思考不可关闭（onlyReasoning），"
                           "已忽略关闭请求，走默认档")
        elif off:
            out.pop("reasoning_effort", None)
            if profile.id == "minimax":
                # 2026-11-04 探针：invalid thinking.type "enabled"
                # (allowed: adaptive, disabled)
                out["thinking"] = {"type": "disabled"}
                actions.append(f"{profile.label} 已发送关闭思考请求（thinking.type=disabled）")
            else:
                actions.append(f"{profile.label} 尚无已验证的关闭方言；已摘除不兼容键，保留厂商默认行为")
        elif profile.id == "minimax" and (
                enable is True or (isinstance(thinking, dict)
                                   and str(thinking.get("type", "")).lower()
                                   in ("adaptive", "enabled", "on", "true"))):
            out["thinking"] = {"type": "adaptive"}
            actions.append(f"{profile.label} 已发送自适应思考请求（thinking.type=adaptive）")
        if not off and effort is not None and profile.reasoning_efforts:
            clamped = _clamp_effort(str(effort), profile.reasoning_efforts)
            if clamped != effort:
                actions.append(f"{profile.label} effort 档位 {effort} 不受支持，"
                               f"钳到 {clamped}（支持：{'/'.join(profile.reasoning_efforts)}）")
                out["reasoning_effort"] = clamped
        if budget is not None:
            actions.append(f"{profile.label} 不认 thinking_budget，已摘除")

    elif style == "thinking_type":
        out.pop("thinking", None)
        out.pop("reasoning_effort", None)
        out.pop("enable_thinking", None)
        out.pop("thinking_budget", None)
        off = _thinking_off(thinking, enable)
        explicit_on = (isinstance(thinking, dict)
                       and str(thinking.get("type", "")).lower()
                       in ("enabled", "on", "true"))
        if off:
            out["thinking"] = {"type": "disabled"}
            actions.append(f"{profile.label} 已发送关闭思考请求（thinking.type=disabled）")
        elif explicit_on:
            out["thinking"] = {"type": "enabled"}
        elif enable is True:
            out["thinking"] = {"type": "enabled"}
            actions.append(f"{profile.label} enable_thinking=true 译作 thinking.type=enabled")
        elif effort is not None and not profile.supports_reasoning_effort:
            actions.append(f"{profile.label} 无 effort 分档（探针实证仅 on/off），"
                           f"reasoning_effort={effort} 已摘除，走厂商默认 auto")
        elif isinstance(thinking, dict) and str(thinking.get("type", "")).lower() in ("auto", "adaptive"):
            actions.append(f"{profile.label} 默认即 auto 思考，请求参数已摘除")
        if effort is not None and profile.supports_reasoning_effort and not off:
            out["reasoning_effort"] = effort
            actions.append(f"{profile.label} 保留显式 reasoning_effort（直连 high 档已有探针实证）")

    elif style == "enable_thinking":
        out.pop("thinking", None)
        effort = out.pop("reasoning_effort", None)
        off = _thinking_off(thinking, enable)
        if off:
            out["enable_thinking"] = False
        elif enable is True:
            out["enable_thinking"] = True
        elif isinstance(thinking, dict) and str(thinking.get("type", "")).lower() in (
                "enabled", "on", "true"):
            out["enable_thinking"] = True
            actions.append(f"{profile.label} thinking.type=enabled 译作 enable_thinking=true")
        if effort is not None:
            if profile.reasoning_efforts:
                out["reasoning_effort"] = _clamp_effort(str(effort), profile.reasoning_efforts)
            else:
                actions.append(f"{profile.label} effort 档位未核实，reasoning_effort 已摘除")

    return out, actions


# --------------------------------------------------------------------------- #
# mixed-fleet helpers
# --------------------------------------------------------------------------- #


def fleet_cache_compatible(profiles: Iterable[VendorProfile]) -> bool:
    """Legacy vendor-homogeneity flag, retained for compatibility.

    This does not prove cache sharing: endpoint, account, model, wire, cache
    mode and exact prefix still isolate namespaces. Request-level telemetry
    in CacheWarmth, not this fleet summary, drives warmth decisions.
    """
    ids = {p.id for p in profiles}
    if ids <= {"local"}:
        return True
    if len(ids - {"local"}) > 1:
        return False
    return True


def describe_fleet(profiles: Iterable[VendorProfile]) -> dict[str, Any]:
    """Summary for the selection UI and for ``forge vendors``."""
    rows = list(profiles)
    ids = [p.id for p in rows]
    unique = list(dict.fromkeys(ids))
    best_cache = min((p.cache_read_multiplier for p in rows), default=1.0)
    return {
        "vendors": unique,
        "vendor_count": len([v for v in unique if v != "local"]),
        "mode": "single" if len([v for v in unique if v != "local"]) <= 1 else "mixed",
        "cache_shared": fleet_cache_compatible(rows),
        "cross_model_cache_guaranteed": False,
        "cache_namespace": ["endpoint", "account", "model", "wire", "cache_mode", "prefix"],
        "best_cache_read_multiplier": best_cache,
        "any_peak_valley": any(p.peak_valley for p in rows),
        "any_long_context_cliff": any(p.long_context_threshold for p in rows),
        "any_explicit_cache": any(p.explicit_cache for p in rows),
        "live_notes": [f"{p.label}: {p.notes}" for p in rows],
    }


__all__ = [
    "CANONICAL_USAGE_KEYS",
    "PROFILES",
    "VENDOR_ORDER",
    "VendorProfile",
    "describe_fleet",
    "fleet_cache_compatible",
    "hit_rate",
    "is_peak",
    "normalize_usage",
    "price_multiplier",
    "profile_for",
    "shape_anthropic",
    "shape_bailian",
    "shape_reasoning",
    "shape_request",
    "should_inject_cache",
]
