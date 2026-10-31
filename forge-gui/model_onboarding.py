"""桌面版模型接入（Coding 上手清单第 ① 步）：选厂商 → 粘贴密钥 → 测试连接 → 保存。

写入格式与 Work Mode「配置API」/ AutoClaw 导入完全一致：
  * 用户层 ``<home>/forge.patch.json`` 追加或覆盖一行
    ``{"id": <vendor>, "name": "provider:<vendor>", "config": {...}}``，
    ``apiKey`` 只存 ``{"$expr": "get('env.FORGE_<ID>_KEY', '')"}`` 引用；
  * 明文密钥只进密钥库（``secret_store``，``~/.forge/secrets.json``）。
Coding 的 ``ModelSettingsSource`` 与 Work Mode 读的都是这两处，保存后两边
同时可见。本模块不依赖 Tk，可单测。
"""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from config_model import load_user_layer, save_user_layer


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    blurb: str
    base_url: str
    models: tuple[str, ...]
    console: str = ""
    wire: str = "openai"
    keyless: bool = False
    badge: str = ""
    color: str = "#2A2F3A"
    letter: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


# 地址与 provider_catalog / Android 引导目录一致（2026-10 核对）；
# 第一项为推荐默认。
VENDORS: tuple[Vendor, ...] = (
    Vendor("deepseek", "DeepSeek", "写代码强·便宜", "https://api.deepseek.com",
           ("deepseek-chat", "deepseek-reasoner"), "https://platform.deepseek.com/api_keys",
           badge="推荐", color="#4D6BFE", letter="D"),
    Vendor("moonshot", "Kimi", "长上下文", "https://api.moonshot.cn/v1",
           ("kimi-k2", "kimi-k2-turbo"), "https://platform.moonshot.cn/console/api-keys",
           color="#1F1F1F", letter="K"),
    Vendor("zhipu", "智谱 GLM", "国内直连", "https://open.bigmodel.cn/api/paas/v4",
           ("GLM-5.3", "glm-4.7"), "https://open.bigmodel.cn/usercenter/apikeys",
           color="#3859FF", letter="智"),
    Vendor("qwen", "通义千问", "阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1",
           ("qwen3-coder-plus", "qwen-plus"), "https://bailian.console.aliyun.com/?apiKey=1",
           color="#615CED", letter="通"),
    Vendor("anthropic", "Claude", "需海外网络", "https://api.anthropic.com",
           ("claude-sonnet-5", "claude-haiku-4-5"), "https://console.anthropic.com/settings/keys",
           wire="anthropic", color="#D97757", letter="A"),
    Vendor("ollama", "本地模型", "Ollama 免密钥", "http://127.0.0.1:11434/v1",
           ("qwen3:8b",), keyless=True, color="#2A2F3A", letter="⌂"),
)


def vendor(vendor_id: str) -> Vendor | None:
    return next((v for v in VENDORS if v.id == vendor_id), None)


def env_name(row_id: str) -> str:
    return "FORGE_" + row_id.upper().replace("-", "_") + "_KEY"


def _is_model_row(row) -> bool:
    if not isinstance(row, dict) or row.get("disabled"):
        return False
    name = str(row.get("name") or "")
    config = row.get("config") if isinstance(row.get("config"), dict) else {}
    return name.startswith("provider:") or "baseURL" in config or "base_url" in config


def configured_models(home: Path) -> list[str]:
    """用户层里已启用的模型行 id（不检查密钥是否有效——那是运行时的事）。"""
    try:
        rows = load_user_layer(Path(home))
    except (OSError, ValueError):
        return []
    return [row["id"] for row in rows if _is_model_row(row)]


def apply_key(home: Path, vendor_id: str, api_key: str, *, model: str | None = None) -> dict:
    """写入（或覆盖）一个厂商的模型行，并把密钥存进密钥库。

    返回 ``{"ok": True, "row_id", "model"}`` 或 ``{"ok": False, "error"}``；
    任何一步失败都不留半截配置：先存密钥，再原子写用户层。
    """
    v = vendor(vendor_id)
    if v is None:
        return {"ok": False, "error": f"未知厂商：{vendor_id}"}
    key = (api_key or "").strip()
    if not v.keyless and not key:
        return {"ok": False, "error": "请先粘贴 API 密钥"}
    if any(ch.isspace() for ch in key):
        return {"ok": False, "error": "密钥里有空格或换行，请重新复制"}
    chosen = model or v.models[0]
    home = Path(home)
    try:
        latest = load_user_layer(home)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"读取模型配置失败：{exc}"}
    rows = copy.deepcopy(latest)
    row = next((r for r in rows if r.get("id") == v.id), None)
    if row is None:
        row = {"id": v.id, "name": f"provider:{v.id}"}
        rows.append(row)
    config = dict(row.get("config") or {})
    config.update(wire=v.wire, baseURL=v.base_url, model=chosen)
    config.setdefault("smallModel", chosen)
    if v.keyless:
        config.pop("apiKey", None)
    else:
        config["apiKey"] = {"$expr": f"get('env.{env_name(v.id)}', '')"}
    row["config"] = config
    row.pop("disabled", None)
    try:
        if not v.keyless:
            import secret_store
            secret_store.set_provider(v.id, key)
        save_user_layer(home, rows, expected_rows=latest)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"保存失败：{exc}"}
    return {"ok": True, "row_id": v.id, "model": chosen}


def test_connection(vendor_id: str, api_key: str, *, timeout: float = 8.0,
                    opener=urllib.request.urlopen) -> dict:
    """列一次模型目录验证地址与密钥（不产生计费调用）。

    在后台线程里调用；``opener`` 可注入以便离线测试。
    """
    v = vendor(vendor_id)
    if v is None:
        return {"ok": False, "error": f"未知厂商：{vendor_id}"}
    key = (api_key or "").strip()
    if not v.keyless and not key:
        return {"ok": False, "error": "请先粘贴 API 密钥"}
    url = v.base_url.rstrip("/") + ("/v1/models" if v.wire == "anthropic" else "/models")
    headers = {"Accept": "application/json"}
    if v.wire == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "error": "密钥无效或没有权限，请检查是否复制完整"}
        return {"ok": False, "error": f"服务返回 {exc.code}，可以先保存，稍后再试"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        hint = "本地 Ollama 没有运行" if v.keyless else "网络连不上该厂商（海外厂商可能需要代理）"
        return {"ok": False, "error": f"{hint}：{reason}"}
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        count = len(data.get("data") or data.get("models") or [])
    except (ValueError, AttributeError):
        count = 0
    return {"ok": True, "message": f"连接成功{f'，可用模型 {count} 个' if count else ''}"}
