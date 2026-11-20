"""Authenticated storage for credentials owned by the Android bridge."""
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from threading import RLock

_KEY = os.environ.pop('FORGE_BRIDGE_VAULT_KEY', '')
_HEADER = b'forge-bridge-vault-v1\n'
_LOCK = RLock()
_memory = {}


class VaultError(RuntimeError):
    pass


def _cipher():
    try:
        from cryptography.fernet import Fernet
        return Fernet(_KEY.encode('ascii'))
    except (ImportError, ValueError, UnicodeError):
        raise VaultError('The bridge credential vault is unavailable') from None


def load(path: Path) -> dict:
    with _LOCK:
        if not path.exists():
            return deepcopy(_memory.get(path, {}))
        if not _KEY:
            raise VaultError('Configure the bridge vault key to load and migrate stored credentials')
        try:
            with path.open('rb') as file:
                raw = file.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('Oversized credential file')
            encrypted = raw.startswith(_HEADER)
            value = json.loads(_cipher().decrypt(raw[len(_HEADER):]) if encrypted else raw)
            if not isinstance(value, dict):
                raise ValueError('Invalid credential data')
            if not encrypted:
                save(path, value)
            return value
        except Exception:
            raise VaultError('Could not read or migrate the bridge credential file') from None


def save(path: Path, value: dict) -> None:
    with _LOCK:
        if not _KEY:
            if path.exists():
                raise VaultError('Configure the bridge vault key before updating stored credentials')
            _memory[path] = deepcopy(value)
            return
        encrypted = _HEADER + _cipher().encrypt(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        if len(encrypted) > 1024 * 1024:
            raise VaultError('The bridge credential file is too large')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.vault-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
