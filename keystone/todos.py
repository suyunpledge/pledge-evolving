"""A turn-local, model-reported checklist. Updates never authorize other tools."""
from .secrets import redact
from .tools import ToolResult, ToolSpec

NAME = 'update_todos'
STATUSES = ('pending', 'in_progress', 'completed', 'blocked')
GUIDANCE = ('For multi-step tasks, publish a short checklist before doing the work with update_todos. '
            'If a pre-execution plan is supplied, start from its tasks. Send the full updated list as '
            'work progresses, with at most one active step. Mark work completed only after you verify it. '
            'Keep unfinished or blocked work visible. Use the user\'s language. Answer simple questions directly.')
SCHEMA = {'type': 'object', 'properties': {'items': {'type': 'array', 'minItems': 1, 'maxItems': 12,
    'items': {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 400},
        'status': {'type': 'string', 'enum': list(STATUSES)}}, 'required': ['text', 'status'],
        'additionalProperties': False}}}, 'required': ['items'], 'additionalProperties': False}


def checked(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError('请提供 1 至 12 项待办。')
    rows = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'text', 'status'} or item['status'] not in STATUSES
                or not isinstance(item['text'], str) or not 1 <= len(item['text'].strip()) <= 400):
            raise ValueError('每项需要有效的 text 和 status。')
        rows.append({'text': redact(item['text'].strip()), 'status': item['status']})
    if sum(row['status'] == 'in_progress' for row in rows) > 1:
        raise ValueError('同一时间只能有一项正在进行。')
    return rows


def from_plan(tasks):
    return checked([{'text': text, 'status': 'pending'} for text in tasks])


def event_items(event):
    if event.get('depth'):
        return None
    try:
        if event.get('type') == 'task_plan':
            return from_plan(event.get('plan', {}).get('tasks', []))
        if event.get('type') == 'tool_completed' and event.get('tool') == NAME and event.get('ok'):
            return checked((event.get('meta') or {}).get('todos'))
    except (ValueError, TypeError, AttributeError):
        pass  # Older journals can contain partial planning events.
    return None


def update(args, _ctx=None):
    try:
        if set(args) != {'items'}:
            raise ValueError('请使用 items 提交完整清单。')
        rows = checked(args['items'])
    except ValueError as exc:
        return ToolResult(False, error=str(exc))
    return ToolResult(True, '待办清单已更新。', meta={'todos': rows})


def install(agent):
    from .policy import ToolEffect
    agent.registry.register(ToolSpec(NAME, GUIDANCE, update, schema=SCHEMA, no_defer=True),
                            trusted=True, effect=ToolEffect.READ)
    if NAME in agent.registry.names():
        agent.extra_system = (agent.extra_system or '') + '\n' + GUIDANCE
