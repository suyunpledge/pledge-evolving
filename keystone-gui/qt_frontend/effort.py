"""思考强度（reasoning effort）档位：当前模型支持哪些档、实际发送哪一档。

桌面端只有四档：``off``（不发送思考参数，按模型默认）/ ``low`` / ``medium`` / ``high``。
档位能否生效看两处，按优先级：

1. 模型行里用户声明的 ``routeCapabilities`` / ``modelCapabilities`` 的
   ``reasoningEfforts``——网关会拒绝列表之外的档位，所以界面只给列表里的档；
2. ``keystone.vendors.profile_for`` 的实测厂商画像——只有开/关思考的厂商
   （thinking_type 且无 effort 分档）只留「默认」，有实测档位的只给这些档。

未识别的端点不做主：四档全给，原样透传。纯函数、无 Qt 依赖，可单测。
"""
from __future__ import annotations

LEVELS = ('off', 'low', 'medium', 'high')
LABELS = {'off': '默认', 'low': '轻量', 'medium': '标准', 'high': '深入'}


def _declared(provider):
    """User-declared effort list for this route+model, or None when undeclared."""
    route = provider.get('routeCapabilities')
    model_caps = provider.get('modelCapabilities')
    model_caps = model_caps.get(provider.get('model', ''), {}) if isinstance(model_caps, dict) else {}
    lists = [c['reasoningEfforts'] for c in (route, model_caps)
             if isinstance(c, dict) and isinstance(c.get('reasoningEfforts'), list)]
    if not lists:
        return None
    out = [str(v) for v in lists[0]]
    for other in lists[1:]:
        out = [v for v in out if v in other]
    return out


def support(provider):
    """``{'levels': [...], 'hint': str, 'supported': bool}`` for one provider route."""
    if not isinstance(provider, dict) or not provider:
        return {'levels': list(LEVELS), 'hint': '', 'supported': True}
    declared = _declared(provider)
    if declared is not None:
        levels = ['off', *(level for level in LEVELS[1:] if level in declared)]
        if len(levels) == 1:
            return {'levels': levels, 'supported': False,
                    'hint': '此模型配置未声明可用的思考强度，按模型默认运行。'}
        return {'levels': levels, 'supported': True,
                'hint': '按模型配置声明的档位：' + '/'.join(declared)}
    try:
        from keystone.vendors import profile_for
        profile = profile_for(base_url=str(provider.get('baseURL', '')), model=str(provider.get('model', '')),
                              wire=str(provider.get('wire', '')), service=str(provider.get('service', '')),
                              model_kind_id=str(provider.get('modelKind', '') or ''))
    except Exception:
        return {'levels': list(LEVELS), 'hint': '', 'supported': True}
    style, efforts = profile.reasoning_style, tuple(profile.reasoning_efforts)
    if style == 'thinking_type' and not profile.supports_reasoning_effort:
        return {'levels': ['off'], 'supported': False,
                'hint': f'{profile.label} 只有开/关思考，没有强度分档；按模型默认运行。'}
    if style == 'enable_thinking' and not efforts:
        return {'levels': ['off'], 'supported': False,
                'hint': f'{profile.label} 的思考档位尚未核实，按模型默认运行。'}
    if style and efforts:
        levels = ['off', *(level for level in LEVELS[1:] if level in efforts)]
        if len(levels) == 1:
            levels = ['off', 'high'] if 'max' in efforts else levels
        return {'levels': levels, 'supported': len(levels) > 1,
                'hint': f'{profile.label} 支持：' + '/'.join(efforts)}
    if style:
        return {'levels': list(LEVELS), 'supported': True, 'hint': f'{profile.label} 支持思考强度分档。'}
    return {'levels': list(LEVELS), 'supported': True, 'hint': '未识别的端点：所选档位原样发送。'}


def effective(level, levels):
    """The level actually sent: the highest supported level not above the choice."""
    if level in levels:
        return level
    if level not in LEVELS:
        return 'off'
    rank = LEVELS.index(level)
    below = [item for item in levels if LEVELS.index(item) <= rank]
    return below[-1] if below else levels[0]


def for_label(rows, label):
    """Support for a model-picker label, resolved through the gateway's own routes."""
    from .gateway_service import chat_model_routes
    route = chat_model_routes(rows).get(label)
    return support(route[1] if route else None)
