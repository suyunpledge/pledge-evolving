"""Configuration: empty root + ordered patch layers.

Design borrowed from DeepSeek Harness:
  * the root tree starts EMPTY, every profile is stated as differences
  * a row is the unit: ``{id, name, config, disabled, inject}``
  * layers are composed in order, later writes win *by id*
  * a patch REPLACES the whole row config (never a deep merge)
  * ``dump_default()`` skips the user layer so a broken user layer can
    never lock the process out of boot (recovery diagnostic)
  * expressions (``{"$expr": "..."}``) are inert while dumping and are only
    evaluated when a row is materialised at boot
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

CONFIG_NAME = "forge.config.json"
USER_LAYER_NAME = "forge.patch.json"

# Env keys accessible to config expressions (beyond FORGE_* which is always allowed).
# Prevents attacker-reachable config layers from exfiltrating arbitrary secrets.
_ENV_WHITELIST: frozenset[str] = frozenset({
    "HOME", "USER", "USERNAME", "SHELL", "PATH", "LANG", "LC_ALL",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
    "COMPUTERNAME", "HOSTNAME", "TERM", "SHELL",
})


class ConfigError(RuntimeError):
    pass


# -- safe expression evaluation ---------------------------------------------
#
# A config layer is attacker-reachable input (a plugin or an overlay can ship
# one), so row expressions are validated against a tiny AST whitelist rather
# than handed to a bare ``eval``. Attribute access is refused outright: that is
# what blocks the usual ``().__class__.__bases__[0].__subclasses__()`` escape.
#
# The whitelist tables now live in forge.guard, shared with the contribution
# source scanner, so extending (or tightening) the ban surface happens in one
# place and the two scanners can never drift apart again.
from .guard import (
    EXPRESSION_ALLOWED_CALLS,
    EXPRESSION_ALLOWED_NAMES,
    EXPRESSION_ALLOWED_NODES,
)

CTX: list[dict[str, Any]] = [{}]


def _dig(node: Any, parts: list[str], default: Any) -> Any:
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def _helper_get(key: Any, default: Any = None, *_, **__) -> Any:
    """``get('a.b.c', default)`` — dotted lookup without attribute access."""
    return _dig(CTX[0], str(key).split("."), default)


_CALL_IMPLEMENTATIONS: dict[str, Callable[..., Any]] = {
    "get": _helper_get,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "len": len,
    "min": min,
    "max": max,
}

# The callable set and the allowed-name set are the guard's; this assert makes
# a divergence a boot-time error rather than a silently widened surface.
assert set(_CALL_IMPLEMENTATIONS) == set(EXPRESSION_ALLOWED_CALLS), (
    "config callables out of sync with forge.guard.EXPRESSION_ALLOWED_CALLS")

_ALLOWED_NODES = EXPRESSION_ALLOWED_NODES
_ALLOWED_NAMES = EXPRESSION_ALLOWED_NAMES
_ALLOWED_CALLS = _CALL_IMPLEMENTATIONS


@dataclass(frozen=True)
class Row:
    id: str
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False
    inject: tuple[str, ...] = ()

    @staticmethod
    def from_raw(raw: dict[str, Any]) -> "Row":
        if "id" not in raw:
            raise ConfigError(f"row is missing 'id': {raw!r}")
        return Row(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            config=dict(raw.get("config") or {}),
            disabled=bool(raw.get("disabled", False)),
            inject=tuple(raw.get("inject") or ()),
        )

    def to_raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id}
        if self.name:
            out["name"] = self.name
        if self.config:
            out["config"] = self.config
        if self.disabled:
            out["disabled"] = True
        if self.inject:
            out["inject"] = list(self.inject)
        return out


def _eval_expr(expr: str, ctx: dict[str, Any]) -> Any:
    """Evaluate a row expression against the boot context, safely."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ConfigError(f"expression {expr!r} is not parseable: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ConfigError(f"expression {expr!r} uses forbidden construct {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_NAMES and node.id not in _ALLOWED_CALLS:
            raise ConfigError(f"expression {expr!r} references unknown name {node.id!r}")
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name) or func.id not in _ALLOWED_CALLS:
                raise ConfigError(f"expression {expr!r} may only call {sorted(_ALLOWED_CALLS)}")

    CTX[0] = ctx
    namespace: dict[str, Any] = {"ctx": ctx, "true": True, "false": False, "null": None,
                                 "none": None, **_ALLOWED_CALLS}
    namespace.update({k: v for k, v in ctx.items() if not k.startswith("_")})
    try:
        return eval(compile(tree, "<config>", "eval"), {"__builtins__": {}}, namespace)  # noqa: S307
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"expression {expr!r} failed: {exc}") from exc


def resolve(value: Any, ctx: dict[str, Any]) -> Any:
    """Recursively materialise ``{"$expr": ...}`` values."""
    if isinstance(value, dict):
        if set(value) == {"$expr"}:
            return _eval_expr(str(value["$expr"]), ctx)
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    return value


def _boot_ctx(ctx: dict[str, Any] | None) -> dict[str, Any]:
    """Default boot context: process env, so ``get('env.X')`` works everywhere.

    2026-09-15 fix: ``active()``/``get()`` used to materialise rows with an
    EMPTY ctx, so ``{"$expr": "get('env.FORGE_DEEPSEEK_KEY', '')"}`` in a
    bundle always resolved to '' — the real root cause behind the "401 auth
    header format" night (the shell-snapshot env was only half the story).
    Callers may still pass their own ctx; None now means "process env".

    2026-09-20 security: env access is now whitelisted to prevent config
    layers (attacker-reachable via plugins/overlays) from exfiltrating
    arbitrary secrets via $expr.
    """
    if ctx is not None:
        return ctx
    env = {k: v for k, v in os.environ.items()
           if k.startswith("FORGE_") or k in _ENV_WHITELIST}
    return {"env": env}


class Config:
    """Composable configuration tree."""

    def __init__(self) -> None:
        self.rows: dict[str, Row] = {}
        self.order: list[str] = []
        self.history: list[str] = []

    # -- composition -----------------------------------------------------
    def apply_patch(self, patch: Iterable[dict[str, Any]], label: str = "layer") -> None:
        patch = list(patch or [])
        for op in patch:
            if "insert" in op:
                for raw in op["insert"]:
                    self._upsert(Row.from_raw(raw), insert=True)
                continue
            if "remove" in op:
                self._remove(str(op["remove"]))
                continue
            self._upsert(Row.from_raw(op), insert=False)
        self.history.append(label)

    def _upsert(self, row: Row, insert: bool) -> None:
        if row.id in self.rows:
            self.rows[row.id] = row  # later write wins, whole row replaced
        else:
            self.rows[row.id] = row
            if insert:
                self.order.append(row.id)
            else:
                self.order.append(row.id)

    def _remove(self, row_id: str) -> None:
        self.rows.pop(row_id, None)
        if row_id in self.order:
            self.order.remove(row_id)

    def merged(self, other: "Config") -> "Config":
        out = Config()
        out.apply_patch([r.to_raw() for r in self._ordered()], label="base")
        out.apply_patch([r.to_raw() for r in other._ordered()], label="overlay")
        return out

    # -- access ----------------------------------------------------------
    def _ordered(self) -> list[Row]:
        return [self.rows[i] for i in self.order if i in self.rows]

    def live_rows(self) -> list[Row]:
        return [r for r in self._ordered() if not r.disabled]

    def row(self, row_id: str) -> Row | None:
        return self.rows.get(row_id)

    def active(self, ctx: dict[str, Any] | None = None) -> list[tuple[str, str, dict[str, Any]]]:
        """Materialise live rows as ``(id, name, config)``."""
        ctx = _boot_ctx(ctx)
        return [(r.id, r.name, resolve(r.config, ctx)) for r in self.live_rows()]

    def get(self, row_id: str, key: str, default: Any = None, ctx: dict[str, Any] | None = None) -> Any:
        row = self.rows.get(row_id)
        if row is None or row.disabled:
            return default
        return resolve(row.config.get(key, default), _boot_ctx(ctx))

    # -- serialisation ---------------------------------------------------
    def dump(self) -> list[dict[str, Any]]:
        return [r.to_raw() for r in self._ordered()]

    def to_json(self) -> str:
        return json.dumps(self.dump(), ensure_ascii=False, indent=2)


def _read_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    stripped = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )
    if not stripped.strip():
        return []
    return json.loads(stripped, object_pairs_hook=_no_dup_keys)


def _no_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"duplicate key {key!r} in config layer")
        out[key] = value
    return out


def load_config(
    home: str | Path,
    *,
    bundles: Iterable[str | Path] = (),
    overlays: Iterable[str | Path] = (),
    include_user_layer: bool = True,
    validate: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> Config:
    """Compose: bundle layers -> user layer -> extra overlays."""
    home = Path(home)
    cfg = Config()
    for bundle in bundles:
        bundle_path = Path(bundle)
        if bundle_path.is_file():
            cfg.apply_patch(_read_json(bundle_path), label=bundle_path.name)
    if include_user_layer:
        user = home / USER_LAYER_NAME
        if user.is_file():
            cfg.apply_patch(_read_json(user), label=USER_LAYER_NAME)
    for overlay in overlays:
        overlay_path = Path(overlay)
        if overlay_path.is_file():
            cfg.apply_patch(_read_json(overlay_path), label=overlay_path.name)
    if validate is not None:
        for row_id, name, conf in cfg.active():
            validate(row_id, name, conf)
    return cfg


def replace_row(cfg: Config, row: Row) -> Config:
    """Convenience for tests: return a copy with one row replaced."""
    out = Config()
    out.apply_patch([r.to_raw() for r in cfg._ordered()], label="copy")
    out.apply_patch([row.to_raw()], label="replace")
    return out


def insert_row(cfg: Config, row: Row, before: str | None = None) -> Config:
    rows = cfg._ordered()
    out = Config()
    if before is None:
        out.apply_patch([r.to_raw() for r in rows], label="copy")
        out.apply_patch([row.to_raw()], label="append")
        return out
    head = [r.to_raw() for r in rows]
    idx = next((i for i, r in enumerate(rows) if r.id == before), len(rows))
    out.apply_patch(head[:idx], label="copy-head")
    out.apply_patch([row.to_raw()], label="insert")
    out.apply_patch(head[idx:], label="copy-tail")
    return out


__all__ = [
    "Config",
    "ConfigError",
    "Row",
    "load_config",
    "resolve",
    "replace_row",
    "insert_row",
    "CONFIG_NAME",
    "USER_LAYER_NAME",
]
