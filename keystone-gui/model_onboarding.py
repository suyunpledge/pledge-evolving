"""桌面版模型接入（Coding 上手清单第 ① 步）：选厂商 → 粘贴密钥 → 测试连接 → 保存。

写入格式与 Work Mode「配置API」/ AutoClaw 导入完全一致：
  * 用户层 ``<home>/keystone.patch.json`` 追加或覆盖一行
    ``{"id": <vendor>, "name": "provider:<vendor>", "config": {...}}``，
    ``apiKey`` 只存 ``{"$expr": "get('env.KEYSTONE_<ID>_KEY', '')"}`` 引用；
  * 明文密钥只进密钥库（``secret_store``，``~/.keystone/secrets.json``）。
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
from urllib.parse import urlsplit

from keystone.secret_http import open_authenticated

from config_model import load_user_layer, save_user_layer
from provider_catalog import BY_KEY


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


def _catalog_vendor(vendor_id, blurb, models, console, *, color, letter):
    preset = BY_KEY[vendor_id]
    return Vendor(vendor_id, preset.name, blurb, preset.plans[0].base_url, models, console,
                  color=color, letter=letter, aliases=preset.aliases)


# 第一项为推荐默认。
VENDORS: tuple[Vendor, ...] = (
    Vendor("deepseek", "DeepSeek", "写代码强·便宜", "https://api.deepseek.com",
           ("deepseek-chat", "deepseek-reasoner"), "https://platform.deepseek.com/api_keys",
           badge="推荐", color="#4D6BFE", letter="D"),
    _catalog_vendor("openai", "GPT 系列模型", ("gpt-6.1-sol",),
                    "https://platform.openai.com/api-keys", color="#367D71", letter="O"),
    Vendor("moonshot", "Kimi", "长上下文", "https://api.moonshot.cn/v1",
           ("kimi-k2", "kimi-k2-turbo"), "https://platform.moonshot.cn/console/api-keys",
           color="#1F1F1F", letter="K"),
    Vendor("zhipu", "智谱 GLM", "国内直连", "https://open.bigmodel.cn/api/paas/v4",
           ("GLM-5.3", "glm-4.7"), "https://open.bigmodel.cn/usercenter/apikeys",
           color="#3859FF", letter="智"),
    _catalog_vendor("qwen", "通义千问 · 多地域", ("qwen3-coder-plus", "qwen-plus"),
                    "https://bailian.console.aliyun.com/?apiKey=1", color="#615CED", letter="通"),
    _catalog_vendor("openrouter", "多厂商模型路由", ("openrouter/auto",),
                    "https://openrouter.ai/keys", color="#695BE8", letter="O"),
    _catalog_vendor("qiniu", "七牛云 AI Token API", ("deepseek-v3",),
                    "https://portal.qiniu.com", color="#1664FF", letter="七"),
    _catalog_vendor("volces", "豆包 · 标准 API 与套餐", ("doubao-seed-2-1-pro-260915",),
                    "https://console.volcengine.com/ark", color="#587AFF", letter="火"),
    _catalog_vendor("siliconflow", "中国内地与国际端点", ("Qwen/Qwen3-8B",),
                    "https://cloud.siliconflow.cn", color="#6E29F6", letter="硅"),
    _catalog_vendor("mistral", "Mistral 与 Codestral", ("mistral-small-latest",),
                    "https://console.mistral.ai", color="#B95508", letter="M"),
    _catalog_vendor("together", "Together AI 开源模型", ("MiniMaxAI/MiniMax-M3.1-Flash",),
                    "https://api.together.ai", color="#4270A5", letter="T"),
    Vendor("anthropic", "Claude", "需海外网络", "https://api.anthropic.com",
           ("claude-sonnet-5", "claude-haiku-4-5"), "https://console.anthropic.com/settings/keys",
           wire="anthropic", color="#D97757", letter="A"),
    Vendor("ollama", "本地模型", "Ollama 免密钥", "http://127.0.0.1:11434/v1",
           ("qwen3:8b",), keyless=True, color="#2A2F3A", letter="⌂"),
)


def vendor(vendor_id: str) -> Vendor | None:
    return next((v for v in VENDORS if v.id == vendor_id), None)


def env_name(row_id: str) -> str:
    return "KEYSTONE_" + row_id.upper().replace("-", "_") + "_KEY"


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


DEFAULT_VENDOR = "deepseek"
DEFAULT_TEMPERATURE = 0.7


def _connection(vendor_id, api_key, base_url):
    v = vendor(vendor_id)
    if v is None:
        raise ValueError("请选择模型厂商。")
    key = api_key.strip()
    if not v.keyless and not key:
        raise ValueError("请先粘贴 API 密钥。")
    check_key(key)
    address = check_address(v.base_url if base_url is None else base_url)
    return v, "" if v.keyless else key, address


def check_key(key: str) -> None:
    if any(ch.isspace() or ord(ch) < 32 for ch in key):
        raise ValueError("密钥里有空格或换行，请重新复制。")


def check_address(base_url: str) -> str:
    """Normalised HTTP(S) API base without credentials, query or fragment."""
    address = str(base_url).strip().rstrip("/")
    try:
        parsed = urlsplit(address)
        parsed.port
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not any(ch.isspace() or ord(ch) < 32 for ch in address)
                 and not any(ch in address for ch in ("?", "#", chr(92))))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请输入有效的 HTTP / HTTPS 接口地址，不含账号、参数或片段。")
    return address


def apply_key(home: Path, vendor_id: str, api_key: str, *, model: str | None = None,
              base_url: str | None = None, temperature: float | str | None = None,
              credential_path: Path | None = None) -> dict:
    """写入（或覆盖）一个厂商的模型行，并把密钥存进密钥库。

    返回 ``{"ok": True, "row_id", "model"}`` 或 ``{"ok": False, "error"}``；
    任何一步失败都不留半截配置：先存密钥，再原子写用户层。
    """
    try:
        v, key, address = _connection(vendor_id, api_key, base_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    chosen = (model or v.models[0]).strip()
    if not chosen or any(ch in chosen for ch in ("\r", "\n", ",")):
        return {"ok": False, "error": "请输入有效的模型 ID。"}
    if temperature is not None:
        try:
            temperature = float(temperature)
            if not 0 <= temperature <= 2:
                raise ValueError
        except (ValueError, TypeError):
            return {"ok": False, "error": "温度需是 0 到 2 之间的数字。"}
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
    config.update(wire=v.wire, baseURL=address, model=chosen)
    if temperature is not None:
        config["temperature"] = temperature
    config.setdefault("smallModel", chosen)
    if v.keyless:
        config.pop("apiKey", None)
    else:
        config["apiKey"] = {"$expr": f"get('env.{env_name(v.id)}', '')"}
    row["config"] = config
    row.pop("disabled", None)
    try:
        import secret_store
        if v.keyless:
            save_user_layer(home, rows, expected_rows=latest)
        else:
            with secret_store.update_provider(v.id, key, path=credential_path):
                save_user_layer(home, rows, expected_rows=latest)
    except (OSError, ValueError):
        return {"ok": False, "error": "保存失败，请检查配置文件并重试。"}
    return {"ok": True, "row_id": v.id, "model": chosen, "wire": v.wire}


def test_connection(vendor_id: str, api_key: str, *, base_url: str | None = None,
                    timeout: float = 8.0, opener=open_authenticated) -> dict:
    """Probe the model list on the selected API base without following redirects."""
    try:
        v, key, address = _connection(vendor_id, api_key, base_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return probe(address, key, wire=v.wire, keyless=v.keyless, timeout=timeout, opener=opener)


def probe(address: str, key: str, *, wire: str = "openai", keyless: bool = False,
          timeout: float = 8.0, opener=open_authenticated) -> dict:
    """GET <base>/models with the given credential; shared by setup and row editing."""
    if wire == "anthropic" and not urlsplit(address).path.endswith("/v1"):
        address += "/v1"
    url = address + "/models"
    headers = {"Accept": "application/json"}
    if wire == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    max_bytes = 4 * 1024 * 1024
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "error": "密钥无效或没有权限，请检查是否复制完整"}
        return {"ok": False, "error": f"服务返回 {exc.code}，可以先保存，稍后再试"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        hint = "本地 Ollama 没有运行" if keyless else "网络连不上该厂商（海外厂商可能需要代理）"
        return {"ok": False, "error": f"{hint}，请检查接口地址。"}
    if len(body) > max_bytes:
        return {"ok": False, "error": "模型列表超过 4 MiB，无法验证。可以先保存配置。"}
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        data = None
    models = data.get("data", data.get("models")) if isinstance(data, dict) and not data.get("error") else None
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                item = item.get("id") or item.get("name") or item.get("model")
            if not isinstance(item, str) or not item.strip():
                break
        else:
            return {"ok": True, "message": f"连接成功，可用模型 {len(models)} 个"}
    return {"ok": False, "error": "接口已响应，但未返回有效的模型列表。请检查接口地址。"}
