"""GUI 用的密钥 store：~/.forge/secrets.json，模式 0600。

存：{provider_id: apiKey}。GUI 启动时读它 → 注入到子进程 env；
矫治器/forge 永不直接读这个文件（保持「密钥不落盘」的契约）。
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

SECRETS_FILE = Path.home() / ".forge" / "secrets.json"


def load() -> dict[str, str]:
    """读 secrets.json；不存在/不可读返回空 dict。"""
    try:
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(secrets: dict[str, str]) -> None:
    """原子写：临时文件 → os.replace。权限 0600（仅 owner 读写）。"""
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows 上 chmod 只读位生效，但留个姿态
    os.replace(tmp, SECRETS_FILE)


def set_provider(name: str, api_key: str) -> None:
    cur = load()
    if api_key:
        cur[name] = api_key
    save(cur)


def get_provider(name: str) -> str | None:
    return load().get(name)


def env_for(secrets: dict[str, str] | None = None) -> dict[str, str]:
    """把 secrets 转成 env 变量名（FORGE_<NAME>_KEY 大写）。"""
    s = secrets if secrets is not None else load()
    out: dict[str, str] = {}
    for name, key in s.items():
        env_name = "FORGE_" + name.upper().replace("-", "_") + "_KEY"
        out[env_name] = key
    return out
