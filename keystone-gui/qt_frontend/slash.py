"""输入框里的斜杠命令（沿用 Tk 版 /new /task /tools /changes /help，并补上 Qt 的新控件）。

``parse`` 是纯函数：识别不了的 ``/xxx`` 返回 None，按普通消息发送（与 Tk 一致）。
"""
from __future__ import annotations

from .effort import LABELS as EFFORT_LABELS

PAGES = {'/tools': 'tools', '/plugins': 'tools', '/config': 'config', '/models': 'config',
         '/files': 'files', '/changes': 'preview', '/knowledge': 'knowledge', '/schedules': 'schedules',
         '/agents': 'agents', '/connectors': 'connectors'}
HELP = [
    ('/new', '新建对话'), ('/task 内容', '交给 Keystone 一个任务'), ('/model 名称', '切换模型'),
    ('/effort 档位', '思考强度：默认/轻量/标准/深入/沉思'), ('/role 角色', '切换角色模板'),
    ('/search', '联网搜索'), ('/tools', '工具与插件'), ('/config', '模型与配置'), ('/files', '文件与项目'),
    ('/changes', '代码变更'), ('/help', '打开命令面板'),
]
_EFFORT_ALIASES = {**{v: k for k, v in EFFORT_LABELS.items()}, **{k: k for k in EFFORT_LABELS},
                   '关闭': 'off', 'default': 'off', 'contemplation': 'contemplate'}


def _pick(query, options):
    """Exact (case-insensitive) match first, then a unique prefix/substring match."""
    lowered = query.lower()
    exact = [o for o in options if o.lower() == lowered]
    if exact:
        return exact[0]
    near = [o for o in options if o.lower().startswith(lowered)] or [o for o in options if lowered in o.lower()]
    return near[0] if len(near) == 1 else None


def parse(text, *, models=(), roles=()):
    """``(action, argument)`` for a local command, an ``('error', message)``, or None to send as chat.

    ``roles`` are ``(id, name)`` pairs.
    """
    text = str(text or '').strip()
    if not text.startswith('/') or '\n' in text:
        return None
    command, _, argument = text.partition(' ')
    command, argument = command.lower(), argument.strip()
    if command in ('/', '/help', '/?'):
        return ('palette', '')
    if command == '/new' and not argument:
        return ('new', '')
    if command == '/search':
        return ('search', argument)
    if command == '/task':
        return ('task', argument)
    if command in PAGES and not argument:
        return ('navigate', PAGES[command])
    if command == '/model':
        if not argument:
            return ('models', '')
        chosen = _pick(argument, list(models))
        return ('model', chosen) if chosen else ('error', f'没有找到唯一匹配「{argument}」的模型。')
    if command == '/effort':
        level = _EFFORT_ALIASES.get(argument.lower()) if argument else None
        return ('effort', level) if level else ('error', '思考强度可选：默认、轻量、标准、深入、沉思。')
    if command == '/role':
        if not argument:
            return ('roles', '')
        if argument in ('通用', 'general', 'none'):
            return ('role', '')
        names = {name: rid for rid, name in roles}
        ids = {rid: rid for rid, _ in roles}
        chosen = _pick(argument, [*ids, *names])
        return ('role', ids.get(chosen) or names.get(chosen)) if chosen else ('error', f'没有找到角色「{argument}」。')
    return None
