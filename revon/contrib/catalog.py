"""revon.contrib.catalog — provider/模型能力矩阵与降级链构建

暴露两个 hooks（全部纯函数：不联网、不读文件、不读系统时钟）：

  catalog     → 目录构建：providers / models / observations / warnings
  build_chain → 按错误类别构建有序降级链

规则要点：

  - hasKey 只按「是否提供了非空 apiKey 字段」判定，密钥内容绝不回显；
  - 同 id 模型出现在多个 provider 时全部保留，键为 "provider/model"；
  - 降级链只在可重试错误（rate_limit / overloaded / timeout / connection）时补齐，
    不可重试错误（bad_request / invalid / 400）只保留 primary，换 provider 是浪费。
"""

from __future__ import annotations

from typing import Any

MODULE_API_VERSION = 1

_WIRE_KINDS = ("openai", "anthropic")
_NON_RETRYABLE_ERRORS = {"bad_request", "invalid", "400"}
_RETRYABLE_ERRORS = {"rate_limit", "overloaded", "timeout", "connection"}


def _as_str(value: Any) -> str | None:
    """只有字符串才有效；其余一律视为缺失。"""
    if isinstance(value, str):
        return value
    return None


def _model_id(model: Any) -> str | None:
    """models 列表元素通常是字符串；兼容 {"id": ...} 形态。"""
    if isinstance(model, str):
        return model
    if isinstance(model, dict):
        return _as_str(model.get("id") or model.get("name"))
    return None


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def catalog(providers: list[dict], observations: list[dict] | None = None) -> dict:
    """构建 provider/模型能力矩阵。

    provider 形如 {"name","baseUrl","wire"("openai"|"anthropic"),"models":[...],
    "defaultModel","smallModel","apiKey"|无}。
    observations 形如 [{"provider","model","ok","error_class","latencyMs"}]，
    用来累计 attempts / failures / lastErrorClass。
    """
    provider_rows: dict[str, dict] = {}
    model_rows: dict[str, dict] = {}
    warnings: list[str] = []

    for entry in providers or []:
        if not isinstance(entry, dict):
            warnings.append("provider entry skipped: not a dict")
            continue
        name = _as_str(entry.get("name"))
        if not name:
            warnings.append("provider entry skipped: missing name")
            continue
        wire = _as_str(entry.get("wire"))
        raw_models = entry.get("models")
        models: list[str] = []
        if isinstance(raw_models, (list, tuple)):
            for item in raw_models:
                mid = _model_id(item)
                if mid and mid not in models:
                    models.append(mid)
        default = _as_str(entry.get("defaultModel"))
        small = _as_str(entry.get("smallModel"))
        # hasKey 只判「是否提供了非空密钥字段」；密钥值绝不进入任何输出。
        has_key = "apiKey" in entry and bool(entry.get("apiKey"))

        provider_rows[name] = {
            "wire": wire,
            "models": list(models),
            "defaultModel": default,
            "smallModel": small,
            "hasKey": has_key,
        }
        if not models:
            warnings.append(f"provider '{name}' has no models")
        if default and default not in models:
            warnings.append(f"provider '{name}': defaultModel '{default}' not in models")
        if wire not in _WIRE_KINDS:
            warnings.append(f"provider '{name}': wire '{wire}' is not openai/anthropic")
        for mid in models:
            model_rows[f"{name}/{mid}"] = {
                "provider": name,
                "id": mid,
                "wire": wire,
                "isSmall": mid == small,
            }

    obs_rows: dict[str, dict] = {}
    for row in observations or []:
        if not isinstance(row, dict):
            continue
        pname = _as_str(row.get("provider"))
        mname = _as_str(row.get("model"))
        if not pname or not mname:
            continue
        key = f"{pname}/{mname}"
        acc = obs_rows.setdefault(key, {"attempts": 0, "failures": 0, "lastErrorClass": None})
        acc["attempts"] += 1
        if not row.get("ok", True):
            acc["failures"] += 1
            acc["lastErrorClass"] = row.get("error_class")

    return {
        "providers": provider_rows,
        "models": model_rows,
        "observations": obs_rows,
        "warnings": warnings,
    }


def build_chain(providers: list[dict], primary: tuple[str, str] | None,
                error_class: str | None, exclude: list[str] | None = None) -> list[tuple[str, str]]:
    """构建有序降级链 [(provider, model), ...]。

    规则：primary 恒排第一；error_class 属不可重试类（bad_request/invalid/400）
    → 只返回 primary；属可重试类（rate_limit/overloaded/timeout/connection）或
    None → 补齐整链；exclude 里的 provider 跳过；同一 (provider, model) 不重复；
    没有 primary 时按 provider 名排序返回完整链。
    """
    err = error_class.strip().lower() if isinstance(error_class, str) else None

    chain: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    if primary is not None:
        if isinstance(primary, (tuple, list)) and len(primary) >= 2:
            pair = (str(primary[0]), str(primary[1]))
        else:
            pair = (str(primary), "")
        chain.append(pair)
        seen.add(pair)

    if err in _NON_RETRYABLE_ERRORS:
        return chain

    excluded = {str(item) for item in (exclude or [])}
    prov_list = [entry for entry in (providers or []) if isinstance(entry, dict)]
    if primary is None:
        prov_list = sorted(prov_list, key=lambda p: _as_str(p.get("name")) or "")
    for entry in prov_list:
        name = _as_str(entry.get("name"))
        if not name or name in excluded:
            continue
        raw_models = entry.get("models")
        if not isinstance(raw_models, (list, tuple)):
            continue
        for item in raw_models:
            mid = _model_id(item)
            if not mid:
                continue
            pair = (name, mid)
            if pair in seen:
                continue
            seen.add(pair)
            chain.append(pair)
    return chain


# ---------------------------------------------------------------------------
# register / selftest
# ---------------------------------------------------------------------------

def register(api) -> dict[str, Any]:
    """加载时调用一次，声明能力并挂上 hooks。"""
    return {
        "name": "catalog",
        "version": "1.0.0",
        "capabilities": ["models.catalog", "models.chain"],
        "hooks": {
            "catalog": catalog,
            "build_chain": build_chain,
        },
        "author": "PI agent",
        "summary": "Provider/model capability matrix with retry-aware fallback chains.",
    }


def selftest() -> list[tuple[str, bool, str]]:
    """离线自检：覆盖任务书列出的全部用例，共 25 条。"""
    manifest = register(None)
    catalog_hook = manifest["hooks"]["catalog"]
    chain_hook = manifest["hooks"]["build_chain"]

    providers = [
        {
            "name": "deepseek",
            "baseUrl": "https://api.deepseek.example",
            "wire": "openai",
            "models": ["deepseek-chat", "deepseek-small"],
            "defaultModel": "deepseek-chat",
            "smallModel": "deepseek-small",
            "apiKey": "sk-very-secret-do-not-echo",
        },
        {
            "name": "claude",
            "baseUrl": "https://api.anthropic.example",
            "wire": "anthropic",
            "models": ["claude-opus", "deepseek-chat"],  # 同名模型跨 provider
            "defaultModel": "claude-opus",
            "smallModel": "claude-haiku",                # 不在 models 里，不应崩
        },
        {
            "name": "broken-wire",
            "baseUrl": "http://localhost:11434",
            "wire": "ollama",
            "models": ["local-model"],
            "defaultModel": "local-model",
        },
        {
            "name": "no-models",
            "baseUrl": "https://example.invalid",
            "wire": "openai",
            "defaultModel": "ghost",
        },
    ]

    cat = catalog_hook(providers)

    cat_obs = catalog_hook(providers, [
        {"provider": "deepseek", "model": "deepseek-chat", "ok": True, "error_class": None, "latencyMs": 123},
        {"provider": "deepseek", "model": "deepseek-chat", "ok": False, "error_class": "timeout", "latencyMs": 0},
        {"provider": "deepseek", "model": "deepseek-chat", "ok": False, "error_class": "rate_limit", "latencyMs": 0},
    ])
    obs_row = cat_obs["observations"].get("deepseek/deepseek-chat", {})

    retry_chain = chain_hook(providers, ("claude", "claude-opus"), "rate_limit")
    badreq_chain = chain_hook(providers, ("claude", "claude-opus"), "bad_request")
    invalid_chain = chain_hook(providers, ("claude", "claude-opus"), "400")
    none_chain = chain_hook(providers, None, None)
    excluded_chain = chain_hook(providers, None, None, exclude=["deepseek"])
    dup_chain = chain_hook(providers + providers, None, None)
    upper_chain = chain_hook(providers, ("claude", "claude-opus"), "RATE_LIMIT")
    unknown_chain = chain_hook(providers, ("claude", "claude-opus"), "server_error")
    none_primary_chain = chain_hook(providers, None, "timeout")
    empty_cat = catalog_hook([], None)
    empty_chain = chain_hook([], None, None)

    expected_full = [
        ("claude", "claude-opus"),
        ("deepseek", "deepseek-chat"),
        ("deepseek", "deepseek-small"),
        ("claude", "deepseek-chat"),
        ("broken-wire", "local-model"),
    ]
    expected_sorted = [
        ("broken-wire", "local-model"),
        ("claude", "claude-opus"),
        ("claude", "deepseek-chat"),
        ("deepseek", "deepseek-chat"),
        ("deepseek", "deepseek-small"),
    ]

    return [
        ("manifest-name-is-catalog", manifest["name"] == "catalog", str(manifest.get("name"))),
        ("manifest-exposes-both-hooks", callable(catalog_hook) and callable(chain_hook), ""),
        ("catalog-builds-provider-rows",
         set(cat["providers"]) == {"deepseek", "claude", "broken-wire", "no-models"},
         str(sorted(cat["providers"]))),
        ("haskey-true-when-key-provided", cat["providers"]["deepseek"]["hasKey"] is True, ""),
        ("haskey-false-when-key-absent", cat["providers"]["claude"]["hasKey"] is False, ""),
        ("api-key-never-echoed", "sk-very-secret-do-not-echo" not in repr(cat), ""),
        ("smallmodel-flagged",
         cat["models"]["deepseek/deepseek-small"]["isSmall"] is True
         and cat["models"]["deepseek/deepseek-chat"]["isSmall"] is False, ""),
        ("same-model-id-across-providers-kept",
         "deepseek/deepseek-chat" in cat["models"] and "claude/deepseek-chat" in cat["models"],
         str(sorted(cat["models"]))),
        ("no-models-warning", any("no-models" in w for w in cat["warnings"]), str(cat["warnings"])),
        ("default-not-in-models-warning", any("ghost" in w for w in cat["warnings"]), str(cat["warnings"])),
        ("bad-wire-warning", any("broken-wire" in w for w in cat["warnings"]), str(cat["warnings"])),
        ("observations-attempts-accumulate", obs_row.get("attempts") == 3, str(obs_row)),
        ("observations-failures-accumulate", obs_row.get("failures") == 2, str(obs_row)),
        ("observations-last-error-class", obs_row.get("lastErrorClass") == "rate_limit", str(obs_row)),
        ("retryable-error-fills-chain", retry_chain == expected_full, str(retry_chain)),
        ("non-retryable-error-primary-only", badreq_chain == [("claude", "claude-opus")], str(badreq_chain)),
        ("http-400-primary-only", invalid_chain == [("claude", "claude-opus")], str(invalid_chain)),
        ("none-error-class-full-chain", none_chain == expected_sorted, str(none_chain)),
        ("no-primary-sorted-by-provider-name",
         none_primary_chain == expected_sorted and none_primary_chain[0] == ("broken-wire", "local-model"),
         str(none_primary_chain)),
        ("exclude-skips-provider", all(pair[0] != "deepseek" for pair in excluded_chain), str(excluded_chain)),
        ("chain-has-no-duplicates", dup_chain == expected_sorted, str(dup_chain)),
        ("error-class-case-insensitive", upper_chain == expected_full, str(upper_chain)),
        ("unknown-error-class-fills-chain", unknown_chain == expected_full, str(unknown_chain)),
        ("empty-catalog-safe",
         empty_cat == {"providers": {}, "models": {}, "observations": {}, "warnings": []}, str(empty_cat)),
        ("empty-chain-safe", empty_chain == [], str(empty_chain)),
    ]


__all__ = ["MODULE_API_VERSION", "register", "selftest"]
