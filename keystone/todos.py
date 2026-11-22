"""A turn-local, model-reported checklist. Updates never authorize other tools."""
import re

from .secrets import redact
from .tools import ToolResult, ToolSpec

NAME = 'update_todos'
STATUSES = ('pending', 'in_progress', 'completed', 'blocked')
_TITLE_REFERENCES = re.compile(r'`[^`\n]+`|[A-Za-z0-9_./-]+\.[A-Za-z][A-Za-z0-9_-]*')
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


def _echo_matches(echo, current):
    """A shortened label must still identify its current slot. Never rename from an echo."""
    titles = [row['text'] for row in current]

    def references(label):
        return {ref.strip('`') for ref in _TITLE_REFERENCES.findall(label)}

    def shortened(label, title):
        if len(label) < 4:
            return False
        if label in title:
            return True
        # Some echoes drop words inside a title. Require the same opening
        # phrase and every remaining character in order in one unique slot.
        if len(label) < 8 or title[:4] != label[:4]:
            return False
        remaining = iter(title[4:])
        return all(char in remaining for char in label[4:])

    refs = [references(title) for title in titles]
    for index, row in enumerate(echo):
        label = row['text']
        if label == titles[index]:
            continue
        if label in titles:
            return False  # exact title of a different slot
        anchors = references(label)
        if anchors:
            matches = [i for i, known in enumerate(refs) if anchors == known]
        else:
            matches = [i for i, title in enumerate(titles) if shortened(label, title)]
        if matches != [index]:
            return False
    return True


def _coding_progress(args, rows):
    if set(args) == {'todos'}:
        args = {'items': args['todos']}
    if 'statuses' not in args:
        return update(args)
    statuses = args['statuses']
    if (set(args) - {'statuses', 'items'} or not rows or not isinstance(statuses, list)
            or len(statuses) != len(rows)):
        return ToolResult(False, error='statuses 需要与当前清单逐项对应。创建或更改清单请使用 items。')

    def echo_statuses(values):
        echoed = checked(values)
        if len(echoed) != len(rows) or not _echo_matches(echoed, rows):
            raise ValueError('清单标题或顺序无法对应。进度请只发状态字符串，改名请单独使用 items。')
        return [row['status'] for row in echoed]

    try:
        if all(isinstance(value, dict) for value in statuses):
            statuses = echo_statuses(statuses)
        if 'items' in args and echo_statuses(args['items']) != statuses:
            raise ValueError('items 和 statuses 的进度不一致。')
    except ValueError as exc:
        return ToolResult(False, error=str(exc))
    return update({'items': [dict(row, status=status) for row, status in zip(rows, statuses)]})


def install(agent, *, current_items=None):
    from .policy import ToolEffect
    handler, schema, guidance = update, SCHEMA, GUIDANCE
    if current_items is not None:
        guidance = ('Progress: statuses alone, e.g. ["completed","in_progress"], in plan order. '
                    'Create or rename: items alone. Batch with independent work. '
                    'Finish with completed statuses and final_answer in one call. Only final_answer is shown: '
                    'include the requested values or findings from verification.')
        schema = {'type': 'object', 'additionalProperties': False, 'properties': {
            'statuses': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                'items': {'type': 'string', 'enum': list(STATUSES)}}, **SCHEMA['properties'],
            'final_answer': {'type': 'string'}}}

        def handler(args, ctx):
            args = dict(args)
            answer = args.pop('final_answer', None)
            rows = current_items(ctx.extras.get('run_id'))
            result = _coding_progress(args, rows)
            if not result.ok:
                return result
            updated = result.meta['todos']
            if answer is not None:
                if not isinstance(answer, str) or not answer.strip() or any(r['status'] != 'completed' for r in updated):
                    return ToolResult(False, error='只在清单已全部完成后提交 final_answer。')
                result.meta['todo_final_answer'] = True
            if updated == rows:
                result.content = '清单状态已保留；接着处理下一步。'
            return result

    agent.registry.register(ToolSpec(NAME, guidance, handler, schema=schema, no_defer=True),
                            trusted=True, effect=ToolEffect.READ)
    if NAME in agent.registry.names():
        note = 'Reuse the saved checklist. Publish changes only.' if current_items is not None else GUIDANCE
        agent.extra_system = (agent.extra_system or '') + '\n' + note
