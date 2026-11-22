"""A turn-local, model-reported checklist. Updates never authorize other tools."""
from .secrets import redact
from .tools import ToolResult, ToolSpec

NAME = 'update_todos'
STATUSES = ('pending', 'in_progress', 'completed', 'blocked')
GUIDANCE = ('Decide whether a checklist would help track dependencies or unfinished work. '
            'Use update_todos when helpful, even without a planning phase. When show_todos is true the '
            'planned checklist is already visible; only publish changes. Send the full updated list as '
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
            plan = event.get('plan', {})
            return from_plan(plan.get('tasks', [])) if plan.get('show_todos', True) else []
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


def install(agent, *, current_items=None):
    from .policy import ToolEffect
    handler, schema, guidance = update, SCHEMA, GUIDANCE
    if current_items is not None:
        guidance = ('Update the visible plan with statuses in task order, e.g. ["completed","in_progress"]. '
                    'Use items only to create or rename steps. Verify completion; at most one active step.')
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {
            'statuses': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                'items': {'type': 'string', 'enum': list(STATUSES)}}, **SCHEMA['properties']}}

        def handler(args, ctx):
            if 'statuses' not in args:
                return update(args, ctx)
            rows = current_items(ctx.extras.get('run_id'))
            statuses = args['statuses']
            if (set(args) != {'statuses'} or not rows or not isinstance(statuses, list)
                    or len(statuses) != len(rows)):
                return ToolResult(False, error='statuses 需要与当前清单逐项对应。创建或更改清单请使用 items。')
            # Seed sometimes echoes full rows under statuses. Accept them only
            # when every title still identifies the current item in that slot.
            if all(isinstance(value, dict) for value in statuses):
                if any(set(value) != {'text', 'status'} or value['text'] != row['text']
                       for value, row in zip(statuses, rows)):
                    return ToolResult(False, error='清单标题已变化。请使用 items 提交新的清单。')
                return update({'items': statuses}, ctx)
            return update({'items': [{'text': row['text'], 'status': status}
                for row, status in zip(rows, statuses)]}, ctx)

    agent.registry.register(ToolSpec(NAME, guidance, handler, schema=schema, no_defer=True),
                            trusted=True, effect=ToolEffect.READ)
    if NAME in agent.registry.names():
        note = ('Use update_todos when a checklist helps or the user requests one. '
                'For a visible plan, send only its status changes using statuses.') if current_items is not None else GUIDANCE
        agent.extra_system = (agent.extra_system or '') + '\n' + note
