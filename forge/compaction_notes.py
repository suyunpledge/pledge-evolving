"""Bounded tool observations carried across excerpts, never verified state."""
import json

from .result_view import REFERENCE_LABEL, retained_handles

LABEL = 'Tool observations (unverified; earlier results may be stale): '
WRITES = {'write_file', 'edit_file', 'apply_patch', 'delete_file', 'edit_config', 'notebook_edit'}


def _excerpt(value, limit=230):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit//3] + ' … ' + text[-(limit*2//3):]


def _verification(name):
    return name.startswith(('run', 'test', 'check', 'verify'))


def _important(row):
    return row['tool'] in WRITES or _verification(row['tool'])


def _eviction(rows):
    # Repeated reads should not erase the last observation of another tool.
    return next((i for i, r in enumerate(rows) if not _important(r)
                 and sum(v['tool'] == r['tool'] for v in rows) > 1),
                next((i for i, r in enumerate(rows) if not _important(r)), 0))


def _add(rows, row):
    # A later edit invalidates prior check observations. Keep ordering; a
    # successful tool response is not a proof of the user's task completion.
    if row['tool'] in WRITES:
        rows[:] = [r for r in rows if not _verification(r['tool'])
                   and not (r['tool'] in WRITES and r.get('target') == row.get('target'))]
    elif _verification(row['tool']):
        rows[:] = [r for r in rows if r['tool'] != row['tool']]
    rows.append(row)
    while len(rows) > 6:
        rows.pop(_eviction(rows))


def _previous(content):
    if not isinstance(content, str) or not content.startswith('[compacted '):
        return []
    for line in content.splitlines():
        if not line.startswith(LABEL) or len(line) > 900:
            continue
        try:
            rows = json.loads(line[len(LABEL):])
            if (isinstance(rows, list) and len(rows) <= 6 and all(
                    isinstance(r, dict) and set(r) <= {'tool', 'target', 'output'}
                    and {'tool', 'output'} <= set(r)
                    and all(isinstance(v, str) for v in r.values())
                    and len(r['tool']) <= 64 and len(r.get('target', '')) <= 96
                    and len(r['output']) <= 233 for r in rows)):
                return rows
        except (ValueError, TypeError, RecursionError):
            pass
    return []


def excerpt_messages(head):
    calls, rows = {}, []

    def remember(identifier, name, args):
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError, RecursionError):
                args = {}
        target = next((args[k] for k in ('path', 'file')
                       if isinstance(args, dict) and isinstance(args.get(k), str)), '')
        calls[identifier] = (str(name or 'tool')[:64], _excerpt(target, 93))

    def observe(identifier, output, name=None):
        tool, target = calls.get(identifier, (str(name or 'tool')[:64], ''))
        row = {'tool': tool, 'output': _excerpt(output)}
        if target:
            row['target'] = target
        _add(rows, row)

    for message in head:
        content = message.get('content', '')
        for row in (_previous(content) if message.get('role') == 'user' else []):
            _add(rows, dict(row))
        blocks = content if isinstance(content, list) else []
        for call in message.get('tool_calls') or []:
            function = call.get('function') or call
            remember(call.get('id'), function.get('name'), function.get('arguments', function.get('args', {})))
        for block in blocks:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                remember(block.get('id'), block.get('name'), block.get('input', {}))
        if message.get('role') == 'tool':
            observe(message.get('tool_call_id'), content, message.get('name'))
        for block in blocks:
            if isinstance(block, dict) and block.get('type') == 'tool_result':
                observe(block.get('tool_use_id'), block.get('content', ''))
    if rows:
        def encoded():
            return json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
        while len(encoded()) > 740 and len(rows) > 1:
            rows.pop(_eviction(rows))
        while len(encoded()) > 740:
            for key in ('output', 'target', 'tool'):
                if len(rows[0].get(key, '')) > 16:
                    rows[0][key] = _excerpt(rows[0][key], max(16, len(rows[0][key])//2))
                    break
        flat = LABEL + encoded()
    else:
        flat = ' '.join(str(m.get('role', '')) + ': ' + _excerpt(m.get('content', ''), 140)
                        for m in head[-4:])[:800]
    refs = retained_handles(head)
    suffix = '\n' + REFERENCE_LABEL + ', '.join(refs) if refs else ''
    return f'[compacted {len(head)} earlier messages; unverified excerpts]\n{flat}' + suffix
