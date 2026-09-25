"""forge 配置模型 + 内置格式矫治器

把"用户粘贴一段乱七八糟的 JSON / 几行 baseURL+key"整理成 forge 可消费的
patch 数组（每条 row 形如 ``{id, name, config}``）。

矫治原则：
  1. 不丢用户写过的任何字段——只会补齐、补命名、做类型校正。
  2. 凭据 apiKey 永远写成 ``{"$expr": "get('env.<NAME>', '')"}``，密钥不落盘。
  3. 矫治结果必须是 ``json.dumps`` 能直接落盘的有效 JSON。
  4. 错误信息可以反馈到 GUI 状态栏，行为必须可预测（同一输入 → 同一输出）。

支持矫治的输入形态（最常见的五种）：
  a. ``[{"id": ..., "config": {...}}, ...]``        —— 标准 patch 数组
  b. ``{"providers": {...}, "routing": {...}}``     —— 语义化分组对象
  c. ``{"provider1": {"baseURL": "...", "apiKey": "..."}}`` —— 纯 provider 表
  d. ``{"id": "p", "wire": "openai", ...}``         —— 单条 row
  e. 一段自由文本（不是 JSON），从中启发式抓出 baseURL / apiKey / model
"""
from __future__ import annotations

import json
import copy
import os
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


# ─── 类型 ────────────────────────────────────────────────


@dataclass
class NormalizeResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rewritten: bool = False  # 是否做了结构性补齐（与原文本差异较大）
    env_hints: dict[str, str] = field(default_factory=dict)  # 建议导出的环境变量名

    def to_json(self) -> str:
        return json.dumps(self.rows, ensure_ascii=False, indent=2)

    def is_valid(self) -> bool:
        return bool(self.rows) and all("id" in r and "config" in r for r in self.rows)


class ConfigNormalizeError(ValueError):
    pass


# ─── 启发式抓取（用于纯文本输入） ──────────────────────────────


# 这些签名尽量宽，能抓住大多数真实粘贴：
#   - https://api.deepseek.com  /  http://127.0.0.1:8787/v1  /  baseUrl: ...
#   - sk-xxxx  /  Bearer xxx  /  API_KEY=xxx
_URL_RE = re.compile(
    r"(?P<url>https?://[^\s\"'<>]+|baseUrl\s*[:=]\s*['\"]?https?://[^\s\"'<>]+)",
    re.IGNORECASE,
)
# 密钥：避免把 markdown 里 "ApiKey=**" 这种误抓
_KEY_RE = re.compile(
    r"(?P<key>(?:api[-_]?key|token|secret|凭据|密钥|key)\s*[:=]\s*['\"]?(?P<v>[A-Za-z0-9._\-+/=]{12,}))",
    re.IGNORECASE,
)
# 常见的 model 名（启发式命名 provider id）
_MODEL_RE = re.compile(
    r"(?P<m>(?:gpt|claude|deepseek|mimo|gemini|qwen|llama|mistral)[\w\-.:]*[a-z0-9])",
    re.IGNORECASE,
)
_WIRE_RE = re.compile(r"(?P<w>openai|anthropic)", re.IGNORECASE)


def _sniff_text(text: str) -> dict[str, str]:
    """从纯文本里抓 baseURL / apiKey / model / wire，启发式。"""
    out: dict[str, str] = {}
    if (m := _URL_RE.search(text)):
        url = m.group("url")
        if url.lower().startswith("baseurl"):
            url = re.split(r"[:=]", url, maxsplit=1)[1].strip().strip("'\"")
        out["baseURL"] = url.rstrip("/")
    if (m := _KEY_RE.search(text)):
        out["apiKey"] = m.group("v").strip()
    if (m := _MODEL_RE.search(text)):
        out["model"] = m.group("m").lower()
    if (m := _WIRE_RE.search(text)):
        out["wire"] = m.group("w").lower()
    return out


# ─── JSON 解析（带 // 行注释剥离，与 forge 一致） ──────────────────


def _read_json_lenient(text: str) -> Any:
    """和 forge.config._read_json 行为一致：剥离 // 行注释后 JSON 解析。"""
    stripped = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    if not stripped.strip():
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        # 容错：用户常把结尾逗号或缺右括号漏掉——给一针人话提示
        raise ConfigNormalizeError(
            f"JSON 解析失败：{exc.msg}（行 {exc.lineno} 列 {exc.colno}）。"
            "常见原因：缺右括号 / 多余逗号 / 中文标点。"
        ) from exc


# ─── 矫治 ──────────────────────────────────────────────


# 安全 id：只允许字母数字下划线连字符，长度 1-32
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_\-]{0,31}$")


def _safe_id(raw: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9_\-]+", "_", (raw or "").lower()).strip("_-")
    if not s:
        return fallback
    if not _SAFE_ID.match(s):
        # 不合规则补一个稳定的后缀
        s = re.sub(r"^[^a-z]+", "", s) or fallback
        s = f"{s[:24]}_{fallback[:6]}"
    return s[:32]


def _infer_wire(conf: dict[str, Any], warnings: list[str]) -> str:
    """根据 baseURL/字段猜 wire 协议。"""
    w = (conf.get("wire") or "").strip().lower()
    if w in ("openai", "anthropic"):
        return w
    url = (conf.get("baseURL") or "").lower()
    if "anthropic" in url or "/v1/messages" in url:
        return "anthropic"
    if "/v1" in url or "openai" in url or "deepseek" in url or url.startswith("http://127"):
        return "openai"
    warnings.append(f"无法从 baseURL 推断协议，默认按 openai 处理（请手动设 wire）")
    return "openai"


def _wrap_secret(value: str, env_var: str | None = None) -> dict[str, Any]:
    """把明文密钥包成 ``{"$expr": ...}``，确保不落盘。"""
    var = env_var or _guess_env_name()
    return {"$expr": f"get('env.{var}', '')"}


def _guess_env_name() -> str:
    """随机稳定后缀，避免不同 provider 都叫 FORGE_API_KEY。"""
    import secrets
    return "FORGE_KEY_" + secrets.token_hex(3).upper()


def _normalize_provider_row(raw: dict[str, Any], warnings: list[str], seen_ids: set[str]) -> dict[str, Any]:
    """单条 provider 行 → 标准 row。"""
    raw_id = raw.get("id") or raw.get("name") or raw.get("provider") or ""
    if not isinstance(raw_id, str):
        raise ConfigNormalizeError("配置 id 必须是文本")
    rid = raw_id if "id" in raw and "config" in raw else _safe_id(raw_id, fallback="provider")
    if rid in seen_ids:
        # 同名碰撞：加序号
        i = 2
        while f"{rid}_{i}" in seen_ids:
            i += 1
        rid = f"{rid}_{i}"
    seen_ids.add(rid)

    conf = copy.deepcopy(raw.get("config") or {})
    # 平铺写法（顶层字段直接当作 config）
    for k in ("wire", "baseURL", "baseUrl", "apiKey", "api_key", "token",
              "model", "smallModel", "notes"):
        if k in raw and k not in conf:
            conf[k] = raw[k]
    # 字段别名
    if "baseUrl" in conf and "baseURL" not in conf:
        conf["baseURL"] = conf.pop("baseUrl")
    if "api_key" in conf and "apiKey" not in conf:
        conf["apiKey"] = conf.pop("api_key")
    for key in ("wire", "baseURL", "model"):
        if key in conf and not isinstance(conf[key], str):
            raise ConfigNormalizeError(f"[{rid}] {key} 必须是文本")

    # 必填：wire + baseURL + model
    if "wire" not in conf:
        conf["wire"] = _infer_wire(conf, warnings)
    if "model" not in conf or not conf["model"]:
        warnings.append(f"[{rid}] 缺少 model 字段——将补 'default'，使用前请修正")
        conf["model"] = "default"

    # apiKey 必须包成 $expr
    api_key = conf.get("apiKey")
    if api_key is None:
        conf["apiKey"] = _wrap_secret("", _guess_env_name())
        warnings.append(f"[{rid}] 缺少 apiKey——已建占位，需手动设密钥后再启用")
    elif isinstance(api_key, str):
        conf["apiKey"] = _wrap_secret(api_key, _guess_env_name())
        warnings.append(f"[{rid}] 明文密钥已转为 $expr 形式（不落盘，请用环境变量 {list(conf['apiKey'].values())[0]} 注入）")
    elif isinstance(api_key, dict) and "$expr" in api_key:
        # 已经是合法形式——放过
        pass
    else:
        warnings.append(f"[{rid}] apiKey 形态无法识别，按空密钥处理")
        conf["apiKey"] = _wrap_secret("", _guess_env_name())

    # smallModel 默认同 model
    if "smallModel" not in conf:
        conf["smallModel"] = conf["model"]

    result = {
        **{k: copy.deepcopy(v) for k, v in raw.items()
           if k not in conf and k not in ("api_key", "token", "apiKey", "baseUrl")},
        "id": rid,
        "name": raw.get("name") or f"provider:{rid}",
        "config": conf,
    }
    if not conf.get("baseURL"):
        warnings.append(f"[{rid}] 缺少 baseURL——将被标为 disabled")
        result["disabled"] = True
        conf["_missing"] = "baseURL"
    return result


def _normalize_model_row(raw: dict[str, Any] | None, warnings: list[str]) -> dict[str, Any]:
    """model router 行。"""
    if raw is None:
        return {
            "id": "model",
            "name": "model:router",
            "config": {
                "primary": [],
                "fallback": [],
                "routing": {"strategy": "balanced", "tiers": []},
                "moa": False,
            },
        }
    conf = dict(raw.get("config") or {})
    # 平铺写法兼容
    for k in ("primary", "fallback", "moa", "moaModels"):
        if k in raw and k not in conf:
            conf[k] = raw[k]
    if "routing" not in conf:
        conf["routing"] = {"strategy": "balanced", "tiers": conf.get("fallback", [])}
    if "strategy" in conf.get("routing", {}):
        conf["routing"]["strategy"] = str(conf["routing"]["strategy"])
    if "primary" not in conf:
        conf["primary"] = []
    if "fallback" not in conf:
        conf["fallback"] = []
    if "moa" not in conf:
        conf["moa"] = False
    return {
        "id": "model",
        "name": raw.get("name") or "model:router",
        "config": conf,
    }


def _normalize_input(value: Any, warnings: list[str], seen_ids: set[str]) -> dict[str, Any] | None:
    """把各种形态输入转成一条 row（返回 None 表示跳过）。"""
    if not isinstance(value, dict):
        return None
    # 已经是标准 row：id + config
    if "id" in value and "config" in value:
        if not isinstance(value["id"], str) or not value["id"].strip():
            raise ConfigNormalizeError("配置 id 必须是非空文本")
        if not isinstance(value["config"], dict):
            raise ConfigNormalizeError("config 必须是对象")
        conf = value["config"]
        is_provider = (str(value.get("name", "")).startswith("provider:")
                       or any(k in conf for k in ("baseURL", "baseUrl", "wire", "apiKey", "api_key")))
        if not is_provider:
            if value["id"] in seen_ids:
                raise ConfigNormalizeError(f"重复配置 id：{value['id']}")
            seen_ids.add(value["id"])
            return copy.deepcopy(value)
        return _normalize_provider_row(value, warnings, seen_ids)
    # 顶层 name + config（无 id）
    if "name" in value and "config" in value and "wire" not in value:
        return None  # 可能是 policy / loop 等非 provider——交给上层按 id 处理
    # 顶层散列：当成 provider config（外面会被 _normalize_provider_row 接管）
    return _normalize_provider_row(value, warnings, seen_ids)


# ─── 顶层入口 ──────────────────────────────────────────────


def normalize(text: str, *, add_model: bool = True) -> NormalizeResult:
    """主入口：把任意输入整理成 patch rows。"""
    text = (text or "").strip()
    if not text:
        raise ConfigNormalizeError("空输入")

    warnings: list[str] = []
    seen_ids: set[str] = set()

    # 1. 试 JSON
    raw: Any
    try:
        raw = _read_json_lenient(text)
    except ConfigNormalizeError:
        if text.lstrip().startswith(("[", "{")):
            raise
        # 2. 启发式抓取
        sniffed = _sniff_text(text)
        if not sniffed.get("baseURL"):
            raise ConfigNormalizeError(
                "不是合法 JSON，也找不到 URL。请至少粘贴含 baseURL + apiKey 的字段。"
            )
        raw = [{
            "id": sniffed.get("model", "custom"),
            "config": sniffed,
        }]
        warnings.append("输入不是 JSON，已按启发式抓取（建议改用 JSON 格式更稳）")

    rows: list[dict[str, Any]] = []

    # 形态 A：标准 patch 数组
    if isinstance(raw, list):
        for item in raw:
            r = _normalize_input(item, warnings, seen_ids)
            if r:
                rows.append(r)

    # 形态 B/C：对象（顶层是 dict）
    elif isinstance(raw, dict):
        # 先看是不是"单条 row"
        if "id" in raw and "config" in raw:
            r = _normalize_input(raw, warnings, seen_ids)
            if r:
                rows.append(r)
        # 形态 C：{"providers": {...}, ...}
        elif "providers" in raw and isinstance(raw["providers"], dict):
            for pid, pconf in raw["providers"].items():
                if not isinstance(pconf, dict):
                    continue
                r = _normalize_provider_row(
                    {"id": pid, "config": pconf}, warnings, seen_ids
                )
                if r:
                    rows.append(r)
        # 形态 C 平铺：{"someprovider": {"baseURL": ..., "apiKey": ...}, ...}
        elif all(isinstance(v, dict) for v in raw.values()):
            for pid, pconf in raw.items():
                # 排除明显是 routing/strategy/notes 这种 meta
                if pid in ("routing", "strategy", "notes", "primary", "fallback"):
                    continue
                r = _normalize_provider_row(
                    {"id": pid, "config": pconf}, warnings, seen_ids
                )
                if r:
                    rows.append(r)
        else:
            # 形态 D：单条平铺
            r = _normalize_input(raw, warnings, seen_ids)
            if r:
                rows.append(r)

    if not rows:
        raise ConfigNormalizeError("无法识别输入——没产出任何 provider row")

    # 自动补一条 model 行（如果还没有）
    has_model = any(r.get("id") == "model" for r in rows)
    if not has_model and add_model:
        rows.append(_normalize_model_row(None, warnings))

    # 自动建议导出 env（仅当所有密钥都是新生成的占位时）
    env_hints: dict[str, str] = {}
    for r in rows:
        if r.get("disabled"):
            continue
        ak = r.get("config", {}).get("apiKey")
        if isinstance(ak, dict) and "$expr" in ak:
            # 提取 var 名
            m = re.search(r"get\('env\.([A-Z0-9_]+)'", ak["$expr"])
            if m:
                env_hints[r["id"]] = m.group(1)

    return NormalizeResult(
        rows=rows,
        warnings=warnings,
        rewritten=bool(warnings),
        env_hints=env_hints,
    )


def merge_with_user_layer(new_rows: list[dict[str, Any]],
                          existing_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """forge 是"整行替换"语义——按 id 合并：existing 在前，new 覆盖。"""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in existing_rows:
        rid = r.get("id")
        if rid and rid not in by_id:
            order.append(rid)
        if rid:
            by_id[rid] = r
    for r in new_rows:
        rid = r.get("id")
        if not rid:
            continue
        if rid not in order:
            order.append(rid)
        by_id[rid] = r
    return [by_id[rid] for rid in order]


def load_user_layer(home: Path) -> list[dict[str, Any]]:
    """读 ``~/.forge/forge.patch.json``，返回 row 列表；缺失返回 []。"""
    from pathlib import Path
    p = Path(home) / "forge.patch.json"
    if not p.is_file():
        return []
    rows = _read_json_lenient(p.read_text(encoding="utf-8-sig"))
    _validate_rows(rows)
    return rows


def _validate_rows(rows: Any) -> None:
    if not isinstance(rows, list):
        raise ConfigNormalizeError("用户层必须是配置条目数组；为保护原文件，未执行保存")
    ids: set[str] = set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not row["id"].strip() or not isinstance(row.get("config", {}), dict)):
            raise ConfigNormalizeError("用户层条目需要非空 id 和对象类型的 config")
        if row["id"] in ids:
            raise ConfigNormalizeError(f"用户层存在重复 id：{row['id']}")
        ids.add(row["id"])


def save_user_layer(home: Path, rows: list[dict[str, Any]], *,
                    expected_rows: list[dict[str, Any]] | None = None) -> None:
    """先写同目录临时文件，再原子替换；拒绝覆盖已变化或损坏的配置。"""
    _validate_rows(rows)
    p = Path(home)
    p.mkdir(parents=True, exist_ok=True)
    fp = p / "forge.patch.json"
    current = load_user_layer(p)
    if expected_rows is not None and current != expected_rows:
        raise ConfigNormalizeError("配置已被其他程序修改，请刷新后重试")
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p,
                                         prefix=".forge-", suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if load_user_layer(p) != current:
            raise ConfigNormalizeError("保存期间配置已变化，请刷新后重试")
        os.replace(temp_path, fp)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
