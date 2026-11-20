"""One place that turns config into LoopLimits, shared by every entry point.

CLI, build_agent, the client server, the VS Code bridge, chat channels and the
Qt Work loop used to read ``loop.*`` on their own: only the CLI honoured the
vendor ``steps_hint``, and each clamped (or did not clamp) differently, so the
same model got a different step budget depending on how it was launched.

Precedence for ``max_steps``: per-request override > ``maxSteps`` declared in
the user layer (forge.patch.json) > vendor ``steps_hint`` when it is larger >
bundle/mode ``maxSteps`` > the LoopLimits default.
"""
from __future__ import annotations

from typing import Any

from .config import USER_LAYER_NAME
from .runtime_types import LoopLimits

# Shared sanity bounds; per-request overrides outside them are rejected by the
# caller (client server), config values are clamped here.
BOUNDS = {'max_steps': (1, 1000), 'max_depth': (0, 16), 'spawn_budget': (0, 1000)}
_KEYS = {'max_steps': 'maxSteps', 'max_depth': 'maxDepth', 'spawn_budget': 'spawnBudget'}
_DEFAULTS = LoopLimits()


def _clamp(name: str, value: Any) -> int:
    low, high = BOUNDS[name]
    return min(high, max(low, int(value)))


def steps_declared_by_user(cfg) -> bool:
    """True only when the user layer itself wrote ``loop.maxSteps``.

    base.json's ``maxSteps: 12`` is the framework default spelled out, not user
    intent, so it must not suppress the vendor steps_hint.
    """
    row = cfg.row("loop") if hasattr(cfg, "row") else None
    return bool(row and "maxSteps" in (getattr(row, "config", None) or {})
                and str(getattr(row, "source", "")) == USER_LAYER_NAME)


def primary_profile(cfg, router=None):
    """Vendor profile of the route that will actually be tried first.

    With a router in hand its real order wins (a named-model or phase router
    may differ from config); otherwise ask SmartRouter, as the CLI always did.
    """
    from .routing import SmartRouter
    from .vendors import profile_for

    order: list[tuple[str, str]] = []
    selected = router if router is not None and hasattr(router, '_order') else None
    if selected is None:
        try:
            selected = SmartRouter.from_config(cfg)
        except Exception:
            selected = None
    if selected is not None:
        try:
            order = list(selected._order(None))
        except Exception:
            order = []
    if not order:
        primary = cfg.get("model", "primary", None)
        if isinstance(primary, (list, tuple)) and primary:
            order = [(str(primary[0]), str(primary[1]) if len(primary) > 1 else "")]
        elif isinstance(primary, str):
            order = [(primary, "")]

    row_id, model = order[0] if order else ("", "")
    providers = getattr(selected, 'providers', None) or {}
    if row_id in providers and hasattr(providers[row_id], 'profile_for_model'):
        return providers[row_id].profile_for_model(model)
    row = cfg.row(row_id) if row_id else None
    conf = dict(row.config) if row is not None else {}
    actual_model = str(model or conf.get("model", conf.get("defaultModel", "")))
    default_model = str(conf.get("model", conf.get("defaultModel", "")))
    return profile_for(
        base_url=str(conf.get("baseURL", conf.get("base_url", ""))),
        model=actual_model,
        service=str(conf.get("service", "")),
        vendor=str(conf.get("vendor", "")),
        wire=str(conf.get("wire", "")),
        model_kind_id=(conf.get("modelKinds") or {}).get(actual_model, "") or
                      (str(conf.get("modelKindId") or "") if actual_model == default_model else ""),
    )


def resolve_max_steps(cfg, profile=None, override: int | None = None) -> tuple[int, str]:
    """Return ``(max_steps, source)``; source is override|user|hint|config."""
    if override is not None:
        return _clamp('max_steps', override), 'override'
    configured = _clamp('max_steps', cfg.get("loop", "maxSteps", _DEFAULTS.max_steps))
    if steps_declared_by_user(cfg):
        return configured, 'user'
    hint = int(getattr(profile, "steps_hint", 0) or 0)
    if hint > configured:
        return _clamp('max_steps', hint), 'hint'
    return configured, 'config'


def resolve_loop_limits(cfg, *, profile=None, router=None, overrides: dict[str, Any] | None = None,
                        context_chars: int | None = None, use_hint: bool = True) -> LoopLimits:
    """Build LoopLimits for any entry point.

    ``profile``/``router`` select the vendor steps_hint (looked up lazily from
    config when neither is given and ``use_hint`` is on). ``overrides`` holds
    per-request values (None entries are ignored). ``context_chars`` is passed
    through untouched when the caller computed its own budget.
    """
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    if use_hint and profile is None and 'max_steps' not in overrides and not steps_declared_by_user(cfg):
        try:
            profile = primary_profile(cfg, router)
        except Exception:
            profile = None
    steps, _ = resolve_max_steps(cfg, profile if use_hint else None, overrides.get('max_steps'))
    values = {name: _clamp(name, overrides[name] if name in overrides
                           else cfg.get("loop", key, getattr(_DEFAULTS, name)))
              for name, key in _KEYS.items() if name != 'max_steps'}
    if context_chars is None:
        context_chars = int(cfg.get("loop", "contextChars", _DEFAULTS.context_chars) or _DEFAULTS.context_chars)
    return LoopLimits(max_steps=steps, context_chars=int(context_chars),
                      wrap_up_on_limit=bool(overrides.get('wrap_up_on_limit',
                                            cfg.get("loop", "wrapUpOnLimit", _DEFAULTS.wrap_up_on_limit))),
                      **values)
