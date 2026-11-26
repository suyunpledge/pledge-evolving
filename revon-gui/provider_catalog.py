# -*- coding: utf-8 -*-
"""供应商预设目录（含订阅套餐端点）。

对标 Cherry Studio / Chatbox 的做法：**每个供应商单独列一条**，而不是让用户
自己在空白框里填 baseURL。每条可以带多个「套餐」——同一个厂商的
标准 API、Coding Plan、Token 套餐往往是**不同的 baseURL**，混在一起会配错。

每个条目的 ``source`` 标明这条 URL 是怎么来的，因为我不想编：

- ``user-config``  直接取自本机正在用的 revon / AutoClaw 配置（最可靠）
- ``docs``         本次查到的官方文档 / 官方仓库说明
- ``unverified``   行业常见值，但我这次没有核到一手来源 —— 界面上会标「待确认」

厂商名与品牌图标复用 ``brand_marks``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlsplit
from revon import opencode

SOURCE_CONFIRMED = "confirmed"     # 用户直接给定的地址（最权威）
SOURCE_USER = "user-config"        # 取自本机正在用的配置
SOURCE_DOCS = "docs"               # 官方文档 / 官方仓库说明
SOURCE_UNVERIFIED = "unverified"   # 未核到一手来源

SOURCE_LABEL = {
    SOURCE_CONFIRMED: "用户确认",
    SOURCE_USER: "",
    SOURCE_DOCS: "官方文档",
    SOURCE_UNVERIFIED: "待确认",
}


@dataclass(frozen=True)
class Plan:
    """一个接入方式（标准 API / 订阅套餐 / 兼容协议）。

    ``usable=False`` 表示这个地址**本框架用不了**（例如协议不被支持），
    界面上会置灰并说明原因——不生成一份配了也跑不通的模板。
    ``source`` 为空表示继承所属供应商的 ``source``。
    """

    label: str
    base_url: str
    wire: str = "openai"
    note: str = ""
    usable: bool = True
    source: str = ""

    def effective_source(self, preset) -> str:
        return self.source or preset.source


@dataclass(frozen=True)
class ProviderPreset:
    """一个供应商及其各种接入方式。"""

    key: str
    name: str
    brand: str = ""
    aliases: tuple[str, ...] = ()
    plans: tuple[Plan, ...] = field(default_factory=tuple)
    source: str = SOURCE_UNVERIFIED
    docs: str = ""

    @property
    def source_label(self) -> str:
        return SOURCE_LABEL.get(self.source, self.source)


PRESETS: tuple[ProviderPreset, ...] = (
    # ── 国内主流 ────────────────────────────────────────────
    ProviderPreset(
        "deepseek", "深度求索 DeepSeek", "deepseek",
        aliases=("deepseek", "深度求索"),
        source=SOURCE_USER, docs="https://platform.deepseek.com",
        plans=(
            Plan("标准 API", "https://api.deepseek.com"),
        ),
    ),
    ProviderPreset(
        "zhipu", "智谱 BigModel", "glm",
        aliases=("zhipu", "bigmodel", "glm", "智谱"),
        source=SOURCE_USER, docs="https://open.bigmodel.cn",
        plans=(
            Plan("标准 API", "https://open.bigmodel.cn/api/paas/v4"),
            Plan("Coding Plan", "https://open.bigmodel.cn/api/coding/paas/v4",
                 note="GLM Coding 订阅专用端点，与标准 API 不同"),
        ),
    ),
    ProviderPreset(
        "qwen", "千问 AI 平台 / 阿里云百炼", "qwen",
        aliases=("qwen", "dashscope", "bailian", "alibaba", "aliyun", "阿里云", "百炼", "千问"),
        source=SOURCE_USER, docs="https://bailian.console.aliyun.com",
        plans=(
            Plan("中国内地", "https://dashscope.aliyuncs.com/compatible-mode/v1", source=SOURCE_DOCS),
            Plan("国际 · 新加坡", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                 note="使用新加坡地域的 API Key", source=SOURCE_DOCS),
            Plan("中国香港", "https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1",
                 note="使用中国香港地域的 API Key", source=SOURCE_DOCS),
            Plan("美国", "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
                 note="使用美国地域的 API Key", source=SOURCE_DOCS),
            Plan("Token Plan · 北京", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", source=SOURCE_USER),
            Plan("千问 Token Plan", "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1", source=SOURCE_USER),
        ),
    ),
    ProviderPreset(
        "moonshot", "月之暗面 Kimi", "kimi",
        aliases=("moonshot", "kimi", "月之暗面"),
        source=SOURCE_USER, docs="https://platform.moonshot.cn",
        plans=(
            Plan("标准 API", "https://api.moonshot.cn/v1"),
            Plan("Kimi Code 订阅（OpenAI 兼容）", "https://api.kimi.com/coding/v1",
                 note="Kimi Code 订阅制端点"),
            Plan("Kimi Code 订阅（Anthropic 兼容）", "https://api.kimi.com/coding",
                 wire="anthropic", note="同一个订阅的 Messages 协议入口"),
        ),
    ),
    ProviderPreset(
        "minimax", "MiniMax 国内", "minimax",
        aliases=("minimax", "海螺"),
        source=SOURCE_USER, docs="https://platform.minimaxi.com",
        plans=(
            Plan("标准 API", "https://api.minimaxi.com/v1"),
            Plan("Coding Plan", "https://api.minimaxi.com/v1",
                 note="订阅套餐与标准 API 同域；是否另有专用路径请以控制台为准"),
        ),
    ),
    ProviderPreset(
        "minimax_global", "MiniMax Global", "minimax",
        aliases=("minimax global", "minimax.io"),
        source=SOURCE_CONFIRMED, docs="https://platform.minimax.io",
        plans=(
            Plan("标准 API", "https://api.minimax.io/v1"),
        ),
    ),
    ProviderPreset(
        "siliconflow", "硅基流动 SiliconFlow", "siliconflow",
        aliases=("siliconflow", "siliconcloud", "silicon", "硅基流动"),
        source=SOURCE_CONFIRMED, docs="https://cloud.siliconflow.cn",
        plans=(
            Plan("中国内地", "https://api.siliconflow.cn/v1", source=SOURCE_DOCS),
            Plan("国际", "https://api.siliconflow.com/v1"),
        ),
    ),
    ProviderPreset(
        "qiniu", "七牛云 AI 推理", "qiniu",
        aliases=("qiniu", "qnaigc", "七牛", "七牛云"),
        source=SOURCE_DOCS, docs="https://developer.qiniu.com/aitokenapi/13379/real-time-ai-interface-api",
        plans=(
            Plan("标准 API", "https://api.qnaigc.com/v1"),
            Plan("Dify 兼容入口", "https://openai.qiniu.com/v1",
                 note="七牛官方 dify-plugin 文档给出的默认 Endpoint"),
            Plan("中转 bypass", "https://api.qnaigc.com/bypass/openai/v1",
                 note="本机已有的中转接入方式", source=SOURCE_USER),
        ),
    ),
    ProviderPreset(
        "volces", "火山方舟 · 豆包", "doubao",
        aliases=("volces", "volcengine", "ark", "doubao", "豆包", "方舟", "火山", "字节"),
        source=SOURCE_USER, docs="https://console.volcengine.com/ark",
        plans=(
            Plan("标准 API", "https://ark.cn-beijing.volces.com/api/v3",
                 note="可填写已开通的模型 ID 或推理接入点 ID", source=SOURCE_DOCS),
            Plan("Coding Plan（OpenAI）", "https://ark.cn-beijing.volces.com/api/coding/v3", source=SOURCE_DOCS),
            Plan("Agent Plan", "https://ark.cn-beijing.volces.com/api/plan/v3",
                 note="使用 Agent Plan 专用 API Key", source=SOURCE_DOCS),
        ),
    ),
    ProviderPreset(
        "xiaomi", "小米 MIMO", "mimo",
        aliases=("mimo", "xiaomi", "小米"),
        source=SOURCE_USER, docs="https://api.xiaomimimo.com",
        plans=(
            Plan("标准 API", "https://api.xiaomimimo.com/v1"),
            Plan("Token Plan · 中国", "https://token-plan-cn.xiaomimimo.com/v1", source=SOURCE_USER),
        ),
    ),
    ProviderPreset(
        "stepfun", "阶跃星辰 StepFun", "stepfun",
        aliases=("stepfun", "step", "阶跃"),
        source=SOURCE_USER, docs="https://platform.stepfun.com",
        plans=(
            Plan("标准 API", "https://api.stepfun.com/v1"),
            Plan("Step Plan 套餐", "https://api.stepfun.com/step_plan/v1",
                 note="本机配置里为此形态"),
        ),
    ),
    ProviderPreset(
        "antling", "蚂蚁百灵 Ling", "ling",
        aliases=("ling", "百灵", "bailing", "tbox"),
        source=SOURCE_USER, docs="https://api.tbox.cn",
        plans=(
            Plan("标准 API", "https://api.tbox.cn/api/llm/v1"),
        ),
    ),
    # ── 海外 ──────────────────────────────────────────────
    ProviderPreset(
        "opencode-go", "OpenCode Go", "opencode",
        aliases=("opencode go", "opencode-go", "Go 订阅"),
        source=SOURCE_DOCS, docs="https://opencode.ai/docs/console/go/",
        plans=(Plan("Go 订阅 · OpenAI 兼容", opencode.GO + "/openai/v1"),
               Plan("Go 订阅 · Anthropic Messages", opencode.GO + "/anthropic/v1", wire="anthropic")),
    ),
    ProviderPreset(
        "opencode-zen", "OpenCode Zen", "opencode",
        aliases=("opencode zen", "opencode-zen", "OpenCode Console", "按量付费"),
        source=SOURCE_DOCS, docs="https://opencode.ai/docs/console/models/",
        plans=(Plan("按量付费 · OpenAI 兼容", opencode.ZEN + "/openai/v1"),
               Plan("按量付费 · Anthropic Messages", opencode.ZEN + "/anthropic/v1", wire="anthropic")),
    ),
    ProviderPreset(
        "openrouter", "OpenRouter", "openrouter",
        aliases=("openrouter", "模型路由", "模型聚合"),
        source=SOURCE_DOCS, docs="https://openrouter.ai/docs/quickstart",
        plans=(Plan("OpenAI 兼容", "https://openrouter.ai/api/v1"),),
    ),
    ProviderPreset(
        "mistral", "Mistral AI", "mistral",
        aliases=("mistral", "codestral"),
        source=SOURCE_DOCS, docs="https://docs.mistral.ai/api/endpoint/chat",
        plans=(Plan("标准 API", "https://api.mistral.ai/v1"),),
    ),
    ProviderPreset(
        "together", "Together AI", "together",
        aliases=("together", "together.ai", "together.xyz"),
        source=SOURCE_DOCS, docs="https://docs.together.ai/docs/quickstart",
        plans=(Plan("标准 API", "https://api.together.ai/v1"),),
    ),
    ProviderPreset(
        "anthropic", "Anthropic Claude", "claude",
        aliases=("anthropic", "claude"),
        source=SOURCE_USER, docs="https://console.anthropic.com",
        plans=(
            Plan("Messages API", "https://api.anthropic.com", wire="anthropic",
                 note="不吃 temperature / top_p，必须走服务端默认"),
        ),
    ),
    ProviderPreset(
        "openai", "OpenAI", "chatgpt",
        aliases=("openai", "chatgpt", "gpt"),
        source=SOURCE_USER, docs="https://platform.openai.com",
        plans=(
            Plan("标准 API", "https://api.openai.com/v1"),
            Plan("Responses API", "https://api.openai.com/v1/responses", source=SOURCE_USER),
        ),
    ),
    ProviderPreset(
        "google", "Google Gemini", "gemini",
        aliases=("google", "gemini", "googleai"),
        source=SOURCE_CONFIRMED, docs="https://aistudio.google.com",
        plans=(
            Plan("Google 原生 · generateContent",
                 "https://generativelanguage.googleapis.com", wire="google", source=SOURCE_DOCS),
            Plan("OpenAI 兼容端点",
                 "https://generativelanguage.googleapis.com/v1beta/openai",
                 source=SOURCE_DOCS),
        ),
    ),
    ProviderPreset(
        "xai", "xAI Grok", "grok",
        aliases=("xai", "grok"),
        source=SOURCE_CONFIRMED, docs="https://console.x.ai",
        plans=(
            Plan("标准 API", "https://api.x.ai/v1"),
        ),
    ),
    # ── 其他常见 ──────────────────────────────────────────
    ProviderPreset(
        "spark", "讯飞星火", "spark",
        aliases=("spark", "iflytek", "星火", "讯飞"),
        source=SOURCE_CONFIRMED, docs="https://console.xfyun.cn",
        plans=(
            Plan("OpenAI 兼容 HTTP 接口", "https://spark-api-open.xf-yun.com/v1",
                 note="旧版 WebSocket 协议 wss://spark-api.xf-yun.com/v4.0/chat "
                      "不是 OpenAI SDK 可用的 base URL"),
        ),
    ),
    ProviderPreset(
        "hunyuan", "腾讯混元", "hunyuan",
        aliases=("hunyuan", "混元", "tencent"),
        source=SOURCE_CONFIRMED, docs="https://cloud.tencent.com/product/hunyuan",
        plans=(
            Plan("OpenAI 兼容", "https://api.hunyuan.cloud.tencent.com/v1"),
        ),
    ),
    ProviderPreset(
        "sensenova", "商汤日日新 SenseNova", "sensenova",
        aliases=("sensenova", "日日新", "商汤", "sensechat"),
        source=SOURCE_CONFIRMED, docs="https://platform.sensenova.cn",
        plans=(
            Plan("Token 计划（推荐）", "https://token.sensenova.cn/v1"),
            Plan("标准兼容模式", "https://api.sensenova.cn/compatible-mode/v2"),
        ),
    ),
)

BY_KEY: dict[str, ProviderPreset] = {p.key: p for p in PRESETS}


def _origin(address: str):
    try:
        parts = urlsplit(address)
        host = parts.hostname
        port = parts.port
        if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None:
            return ""
        host = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += f":{port}"
        return f"{parts.scheme}://{host}"
    except ValueError:
        return ""


_ORIGINS = {_origin(plan.base_url): preset for preset in PRESETS for plan in preset.plans
            if _origin(plan.base_url) != opencode.ORIGIN}

# Additional public origins from Cherry Studio 2.1.8's bundled provider registry.
# These labels identify a configured route. They do not select a protocol or create a preset.
_PROVIDER_LABELS = {
    "https://ai-gateway.vercel.sh": "Vercel AI Gateway",
    "https://ai.burncloud.com": "BurnCloud",
    "https://aihubmix.com": "AiHubMix",
    "https://api-inference.modelscope.cn": "ModelScope",
    "https://api.302.ai": "302.AI",
    "https://api.aiionly.com": "AIOnly",
    "https://api.baichuan-ai.com": "百川 Baichuan",
    "https://api.cerebras.ai": "Cerebras AI",
    "https://api.fireworks.ai": "Fireworks",
    "https://api.githubcopilot.com": "GitHub Copilot",
    "https://api.groq.com": "Groq",
    "https://api.jina.ai": "Jina",
    "https://api.longcat.chat": "LongCat",
    "https://api.moonshot.ai": "Moonshot 国际",
    "https://api.ocoolai.com": "ocoolAI",
    "https://api.perplexity.ai": "Perplexity",
    "https://api.poe.com": "Poe",
    "https://api.ppinfra.com": "PPIO",
    "https://api.voyageai.com": "Voyage AI",
    "https://api.z.ai": "Z.ai",
    "https://chatgpt.com": "OpenAI · ChatGPT",
    "https://cli-chat-proxy.grok.com": "Grok CLI",
    "https://deepseek.alayanew.com": "AlayaNew",
    "https://developer.amd.com.cn": "AMD GPU Cloud",
    "https://integrate.api.nvidia.com": "NVIDIA",
    "https://maas-api.lanyun.net": "LANYUN",
    "https://open.cherryin.net": "CherryIN",
    "https://opencode.ai": "OpenCode",
    "https://ph8.co": "PH8",
    "https://qianfan.baidubce.com": "百度千帆",
    "https://router.huggingface.co": "Hugging Face",
    "https://tokendance.space": "TokenDance",
    "https://tokenhub.tencentmaas.com": "腾讯 TokenHub",
    "https://wishub-x1.ctyun.cn": "天翼云息壤",
    "https://www.dmxapi.cn": "DMXAPI",
    "https://www.sophnet.com": "SophNet",
}


def route_identity(address: str, wire: str = "") -> dict:
    """Identify the access platform from the configured origin, independently of the model ID."""
    origin = _origin(address)
    preset = _ORIGINS.get(origin)
    if origin == opencode.ORIGIN:
        plan = opencode.plan_root(address)
        if plan:
            preset = BY_KEY["opencode-go" if plan == opencode.GO else "opencode-zen"]
    if preset is None:
        host = urlsplit(origin).netloc
        return {"providerKey": "custom:" + origin, "providerName": _PROVIDER_LABELS.get(origin, host or "未知供应商"),
                "providerBrandKey": "", "planLabel": ""}
    path = urlsplit(address).path.rstrip("/")
    matches = [plan for plan in preset.plans if _origin(plan.base_url) == origin
               and urlsplit(plan.base_url).path.rstrip("/") == path and (not wire or plan.wire == wire)]
    return {"providerKey": preset.key, "providerName": preset.name, "providerBrandKey": preset.brand,
            "planLabel": matches[0].label if len(matches) == 1 else ""}


def route_fields(config: dict) -> dict:
    """Safe list and card fields shared by the workspace and model settings."""
    from brand_marks import detect, mark_path
    from revon.openai_responses import uses_responses
    from revon.provider_requests import safe_url
    from revon.secrets import redact

    wire = str(config.get("wire") or "openai")
    model = str(config.get("model") or "")
    address = safe_url(config.get("baseURL"))
    identity = route_identity(address, wire)
    brand = detect(model=model.rsplit("/", 1)[-1])
    icon = mark_path(identity["providerBrandKey"], 32) if identity["providerBrandKey"] else None
    protocol = {"openai": "OpenAI 兼容", "anthropic": "Anthropic Messages",
                "google": "Google 原生 · generateContent"}.get(wire, "未知协议")
    transport = SimpleNamespace(base_url=address, wire=wire, adapt=bool(config.get("adapt", True)))
    if uses_responses({"model": model}, transport):
        protocol = "OpenAI Responses"
    note = config.get("providerLabel")
    return {**identity, "providerIcon": icon.as_uri() if icon else "",
            "modelBrand": brand.label if brand else "", "protocolLabel": protocol,
            "displayBaseURL": address, "providerLabel": redact(note) if isinstance(note, str) else ""}


def all_plans() -> list[tuple[ProviderPreset, Plan]]:
    """拉平成 (供应商, 套餐) 列表，供界面逐行展示。"""
    out: list[tuple[ProviderPreset, Plan]] = []
    for preset in PRESETS:
        for plan in preset.plans:
            out.append((preset, plan))
    return out


def config_snippet(preset: ProviderPreset, plan: Plan,
                   model: str = "") -> str:
    """生成可以直接粘进「整理并预览」的配置片段。

    只带 baseURL / wire / model，**不带任何密钥**——密钥由用户自己填或走密钥面板。
    """
    if not plan.usable:
        raise ValueError("该接入方式本框架不可用，不应生成模板")
    lines = [
        f"# 供应商：{preset.name} · {plan.label}",
        f"baseURL: {plan.base_url}",
        f"wire: {plan.wire}",
    ]
    if plan.note:
        lines.append(f"# 备注：{plan.note}")
    source = plan.effective_source(preset)
    if source == SOURCE_UNVERIFIED:
        lines.append("# 注意：该地址未经核实，若报错请以官网控制台为准")
    elif source == SOURCE_CONFIRMED:
        lines.append("# 地址已由使用者确认")
    lines.append("model: " + (model or ""))
    lines.append("apiKey: ")
    return "\n".join(lines)


def find(query: str) -> list[ProviderPreset]:
    """按名称/别名/端点模糊匹配，供搜索框用。"""
    q = str(query or "").strip().lower()
    if not q:
        return list(PRESETS)
    hits = []
    for preset in PRESETS:
        haystack = " ".join((preset.name, preset.key) + preset.aliases
                            + tuple(p.base_url for p in preset.plans)).lower()
        if q in haystack:
            hits.append(preset)
    return hits
