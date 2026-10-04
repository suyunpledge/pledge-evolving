# -*- coding: utf-8 -*-
"""模型品牌标志（brand marks）。

把「模型显示名 / 真实 model id / provider 地址」自动映射到品牌，
并加载构建期生成好的官方标志位图。

三条设计原则：

1. **运行时零依赖**：标志是构建期用 Pillow 生成的 PNG（16/20/24/32 四档），
   运行时只用 ``tk.PhotoImage`` + ``subsample``，不 import Pillow。
   （与品牌头像 ``forge-logo-*.png`` 走同一条路。）
2. **识别有优先级**：模型名精确匹配 → provider 域名 → 模型名子串 → provider id 前缀。
   识别不到就不显示图标——不猜、不自绘山寨标志。
3. **品牌表是数据**：加一家厂商只加一条 ``Brand``，不改任何逻辑。
"""
from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path

# ─── 品牌表 ────────────────────────────────────────────────

GENERATED_SIZES = (16, 20, 24, 32)


@dataclass(frozen=True)
class Brand:
    """一个模型品牌。"""

    key: str
    label: str
    icon: str
    color: str
    aliases: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    id_prefixes: tuple[str, ...] = field(default=())

    def __str__(self) -> str:  # 方便 tooltip 里直接插值
        return self.label


BRANDS: tuple[Brand, ...] = (
    Brand("chatgpt", "ChatGPT", "openai", "#10A37F",
          aliases=("chatgpt", "openai", "gpt", "davinci"),
          domains=("api.openai.com", "openai.com", "openai.azure.com"),
          id_prefixes=("openai",)),
    Brand("claude", "Claude", "claude", "#D97757",
          aliases=("claude", "anthropic", "sonnet", "opus", "haiku"),
          domains=("anthropic.com", "claude.ai"),
          id_prefixes=("anthropic",)),
    Brand("gemini", "Gemini", "gemini", "#4E86FF",
          aliases=("gemini", "bard", "palm"),
          domains=("googleapis.com", "generativelanguage", "google.com"),
          id_prefixes=("google", "gemini")),
    Brand("grok", "Grok", "grok", "#E9E9F0",
          aliases=("grok", "xai"),
          domains=("api.x.ai", "x.ai"),
          id_prefixes=("xai",)),
    Brand("deepseek", "DeepSeek", "deepseek", "#4D6BFE",
          aliases=("deepseek", "深度求索"),
          domains=("api.deepseek.com", "deepseek.com"),
          id_prefixes=("deepseek",)),
    Brand("qwen", "千问", "qwen", "#615CED",
          aliases=("qwen", "千问", "通义", "tongyi", "bailian", "百炼"),
          domains=("dashscope.aliyuncs.com", "aliyuncs.com"),
          id_prefixes=("qwen", "dashscope")),
    Brand("kimi", "Kimi", "kimi", "#E9E9F0",
          aliases=("kimi", "moonshot", "月之暗面"),
          domains=("moonshot.cn", "moonshot.com"),
          id_prefixes=("moonshot", "kimi")),
    Brand("doubao", "豆包", "doubao", "#2E7CF6",
          aliases=("doubao", "豆包", "volcengine", "火山", "方舟", "seed"),
          domains=("volces.com", "volcengine.com"),
          id_prefixes=("doubao", "volc")),
    Brand("minimax", "MiniMax", "minimax", "#E7452D",
          aliases=("minimax", "hailuo", "海螺"),
          domains=("minimaxi.com", "minimax.io", "minimax.com"),
          id_prefixes=("minimax",)),
    Brand("mimo", "MIMO · 小米", "xiaomimimo", "#FF6900",
          aliases=("mimo", "xiaomi", "小米"),
          domains=("xiaomimimo.com", "xiaomi.com"),
          id_prefixes=("mimo", "xiaomi")),
    Brand("ling", "百灵", "antgroup", "#1677FF",
          aliases=("bailing", "百灵", "ling"),
          domains=("tbox.cn", "antgroup.com"),
          id_prefixes=("ant",)),
    Brand("muse", "Muse · Meta", "metaai", "#0866FF",
          aliases=("muse", "meta", "glimmer"),
          domains=("meta.com", "llama.com"),
          id_prefixes=("meta", "muse")),
    Brand("glm", "智谱 GLM", "zhipu", "#2E5CE6",
          aliases=("chatglm", "glm", "zhipu", "智谱", "zai"),
          domains=("bigmodel.cn", "zhipuai.cn", "z.ai"),
          id_prefixes=("zai", "zhipu", "glm")),
    Brand("spark", "讯飞星火", "spark", "#2B7FFF",
          aliases=("spark", "星火", "iflytek", "讯飞"),
          domains=("xfyun.cn", "iflytek.com"),
          id_prefixes=("iflytek", "spark")),
    Brand("stepfun", "阶跃星辰", "stepfun", "#1F7AEC",
          aliases=("stepfun", "step", "阶跃"),
          domains=("stepfun.com",),
          id_prefixes=("stepfun", "step")),
    Brand("hunyuan", "腾讯混元", "hunyuan", "#0052D9",
          aliases=("hunyuan", "混元"),
          domains=("hunyuan.tencent.com", "tencent.com"),
          id_prefixes=("hunyuan", "tencent")),
    Brand("ernie", "文心一言", "baidu", "#2932E1",
          aliases=("ernie", "文心", "baidu", "千帆"),
          domains=("baidubce.com", "baidu.com"),
          id_prefixes=("ernie", "baidu", "qianfan")),
    Brand("sensenova", "商汤日日新", "sensenova", "#1E4FE0",
          aliases=("sensenova", "sensechat", "日日新"),
          domains=("sensenova.cn",),
          id_prefixes=("sensenova", "sense")),
    Brand("mistral", "Mistral", "mistral", "#FA520F",
          aliases=("mistral", "mixtral", "codestral"),
          domains=("mistral.ai",),
          id_prefixes=("mistral",)),
    Brand("ollama", "Ollama · 本地", "ollama", "#E9E9F0",
          aliases=("ollama",),
          domains=("11434",),
          id_prefixes=("ollama",)),
    Brand("siliconflow", "硅基流动 SiliconFlow", "siliconcloud", "#6E29F6",
          aliases=("siliconflow", "siliconcloud", "硅基流动"),
          domains=("siliconflow.cn", "siliconflow.com"),
          id_prefixes=("siliconflow", "siliconcloud")),
    Brand("qiniu", "七牛云", "qiniu", "#1664FF",
          aliases=("qiniu", "七牛", "七牛云", "qnaigc"),
          domains=("qiniu.com", "qnaigc.com"),
          id_prefixes=("qiniu", "qnaigc")),
)

BY_KEY: dict[str, Brand] = {b.key: b for b in BRANDS}

# 别名→品牌；子串匹配时取最长别名，避免 "meta" 抢走 "minimax" 这类误判。
_ALIASES: list[tuple[str, Brand]] = sorted(
    ((alias.lower(), b) for b in BRANDS for alias in b.aliases),
    key=lambda pair: len(pair[0]), reverse=True)

_MIN_SUBSTRING = 3          # 拉丁别名少于 3 个字符只允许精确匹配
_MIN_CJK_SUBSTRING = 2      # 中文两字就已经很特异（千问 / 豆包 / 百灵）


def _matchable(alias: str) -> bool:
    """这个别名能不能参与子串匹配。

    中文不能用拉丁文的长度门槛：两字中文（千问）比四字拉丁（qwen）更特异。
    """
    if any(ord(ch) > 127 for ch in alias):
        return len(alias) >= _MIN_CJK_SUBSTRING
    return len(alias) >= _MIN_SUBSTRING


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


# ─── 识别 ─────────────────────────────────────────────────


def detect(model: object = None, provider: object = None,
           base_url: object = None) -> Brand | None:
    """自动识别品牌。任何一层命中即返回；都命中不了返回 None。

    ``provider`` 可以是 provider 的 config dict（读 id / notes），也可以是 id 字符串。
    """
    conf: dict = provider if isinstance(provider, dict) else {}
    pid = conf.get("id") if conf else (provider if isinstance(provider, str) else "")
    name = _norm(model) or _norm(conf.get("model") or conf.get("modelLabel"))
    url = _norm(base_url) or _norm(conf.get("baseURL"))

    if not name and not url and not pid:
        return None

    # 1) 模型名精确匹配别名——最可信
    for alias, brand in _ALIASES:
        if name and name == alias:
            return brand

    # 2) provider 域名
    if url:
        for brand in BRANDS:
            if any(d in url for d in brand.domains):
                return brand

    # 3) 模型名子串：先看「以它开头」的，再看出现在中间的。
    #    前缀优先是必要的——"muse-spark" 应该算 Muse，不是 Spark。
    if name:
        for alias, brand in _ALIASES:
            if _matchable(alias) and name.startswith(alias):
                return brand
        for alias, brand in _ALIASES:
            if _matchable(alias) and alias in name:
                return brand

    # 4) provider id 前缀
    pid_l = _norm(pid)
    if pid_l:
        for brand in BRANDS:
            if any(pid_l.startswith(p) for p in brand.id_prefixes):
                return brand

    return None


def label_for(model: object = None, provider: object = None,
              base_url: object = None) -> str:
    """品牌显示名；识别不到返回空串。"""
    brand = detect(model=model, provider=provider, base_url=base_url)
    return brand.label if brand else ""


# ─── 位图加载（运行时零依赖）─────────────────────────────


def _asset_dirs() -> list[Path]:
    """与 forge_gui_v2._asset_dirs 同源：打包态先找 _MEIPASS。"""
    here = Path(__file__).resolve().parent
    dirs: list[Path] = []
    base = getattr(sys, "_MEIPASS", "")
    if base:
        dirs.append(Path(base) / "assets")
    dirs.append(here / "assets")
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "assets")
    return dirs


def mark_path(brand: Brand | str, size: int) -> Path | None:
    key = brand.key if isinstance(brand, Brand) else str(brand)
    for d in _asset_dirs():
        p = d / "brands" / f"{key}-{size}.png"
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


_ICON_CACHE: dict[tuple[str, int], tk.PhotoImage] = {}
_CACHE_ROOT: object = None


def _default_root():
    """当前 Tk 默认根。

    Tk 的 PhotoImage 没有显式 master 时挂在「默认根」上；测试（或重启窗口）会
    销毁旧根再建新根，此时旧根上的位图就作废了。缓存必须挂靠根的身份，
    换根就整体丢弃——否则会拿到已销毁的 pyimageNNN，报
    ``image "pyimage123" doesn't exist``。
    """
    return getattr(tk, "_default_root", None)


def _sync_cache_root(master=None) -> None:
    global _CACHE_ROOT
    root = master._root() if master is not None else _default_root()
    if root is not _CACHE_ROOT:
        _ICON_CACHE.clear()
        _CACHE_ROOT = root


def mark_icon(brand: Brand | str | None, size: int = 16, *, master=None):
    """取品牌标志位图。

    返回 ``(PhotoImage | None, keepalive)``：Tk 的 PhotoImage 必须在 Python 侧留引用，
    否则会被 GC 掉变成空白（与 ``load_brand_logo`` 同一个坑）。
    """
    if brand is None:
        return None, None
    _sync_cache_root(master)
    key = brand.key if isinstance(brand, Brand) else str(brand)
    for candidate in (size, 20, 24, 16, 32):
        cached = _ICON_CACHE.get((key, candidate))
        if cached is not None:
            return cached, cached
    for candidate in (size, 20, 24, 16, 32):
        path = mark_path(key, candidate)
        if path is None:
            continue
        try:
            img = tk.PhotoImage(master=master, file=str(path))
        except tk.TclError:
            continue
        _ICON_CACHE[(key, candidate)] = img
        return img, img
    return None, None


def clear_cache() -> None:
    global _CACHE_ROOT
    _ICON_CACHE.clear()
    _CACHE_ROOT = None
