"""GUI 用的密钥 store：~/.keystone/secrets.json，模式 0600。

存：{provider_id: apiKey}。GUI 启动时读它 → 注入到子进程 env；
矫治器/keystone 永不直接读这个文件（保持「密钥不落盘」的契约）。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from keystone.secrets import SecretScope, protect_store_path
from keystone.compat import default_home, mirror_env

SECRETS_FILE = default_home() / "secrets.json"
_LOCK = threading.RLock()


def _valid(secrets):
    return isinstance(secrets, dict) and all(
        isinstance(name, str) and name.strip() and isinstance(key, str)
        for name, key in secrets.items())


def load(*, strict=False, path=None) -> dict[str, str]:
    """读 secrets.json；不存在/不可读返回空 dict。"""
    path = Path(path) if path is not None else SECRETS_FILE
    try:
        protect_store_path(path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not _valid(data):
            raise ValueError("密钥文件须为名称和密钥均为文本的对象")
        scope = SecretScope()
        for key in data.values():
            scope.reference(key)
        scope.close()
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise OSError("密钥文件无法读取或格式损坏，原文件已保留") from exc
        return {}


def save(secrets: dict[str, str], *, path=None) -> None:
    """原子写：临时文件 → os.replace。权限 0600（仅 owner 读写）。"""
    if not _valid(secrets):
        raise OSError("密钥名称和内容必须为文本")
    scope = SecretScope()
    for key in secrets.values(): scope.reference(key)
    scope.close()
    path = Path(path) if path is not None else SECRETS_FILE
    with _LOCK:
        current = load(strict=True, path=path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".secrets-", suffix=".tmp", delete=False) as stream:
                tmp = Path(stream.name)
                protect_store_path(tmp)
                try:
                    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass  # POSIX owner permissions; Windows ACLs are managed by the OS.
                json.dump(secrets, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            if load(strict=True, path=path) != current:
                raise OSError("密钥文件已被其他程序修改，请刷新后重试")
            os.replace(tmp, path)
            protect_store_path(path)
            scope = SecretScope()
            for key in secrets.values(): scope.reference(key)
            scope.close()
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def set_provider(name: str, api_key: str) -> None:
    with _LOCK:
        cur = load(strict=True)
        if api_key:
            cur[name] = api_key
        save(cur)


@contextmanager
def update_provider(name, key, *, path=None):
    """Restore the prior credential if saving its provider row fails."""
    with _LOCK:
        original = load(strict=True, path=path)
        updated = {**original, name: key}
        if updated != original:
            save(updated, path=path)
        try:
            yield
        except Exception:
            if updated != original:
                latest = load(strict=True, path=path)
                if latest.get(name) == key:
                    latest.pop(name)
                    if name in original:
                        latest[name] = original[name]
                    save(latest, path=path)
            raise


def get_provider(name: str) -> str | None:
    return load().get(name)


def delete_provider(name: str) -> None:
    """Remove one credential without racing another local credential update."""
    with _LOCK:
        current=load(strict=True)
        current.pop(name,None)
        save(current)


def env_for(secrets: dict[str, str] | None = None) -> dict[str, str]:
    """把 secrets 转成 env 变量名（KEYSTONE_<NAME>_KEY 大写）。"""
    s = secrets if secrets is not None else load()
    out: dict[str, str] = {}
    for name, key in s.items():
        if isinstance(name,str) and name.startswith('connector.'):
            continue  # Connector credentials never enter model/gateway environments.
        if not isinstance(name, str) or not isinstance(key, str):
            continue
        env_name = "KEYSTONE_" + name.upper().replace("-", "_") + "_KEY"
        out[env_name] = key
    # 旧名 FORGE_<NAME>_KEY 同时提供，用户层里旧的 env.FORGE_* 引用照常解析。
    return dict(mirror_env(out))
