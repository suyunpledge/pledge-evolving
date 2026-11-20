"""Deterministic JSON bytes used by cache keys and runtime evidence.

The cache key must not depend on dictionary insertion order, optional ``None``
fields, or the platform's handling of non-finite numbers.  This module keeps
that policy in one small, dependency-free place so callers do not each grow a
slightly different ``json.dumps`` spelling.
"""

from __future__ import annotations

import json
from typing import Any


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    if isinstance(value, tuple):
        return [_without_none(item) for item in value]
    return value


def dumps(value: Any) -> str:
    """Serialize ``value`` with stable keys, UTF-8 text, and strict numbers."""
    return json.dumps(
        _without_none(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = ["dumps"]
