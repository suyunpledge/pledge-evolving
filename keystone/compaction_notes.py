"""Bounded tool observations carried across excerpts, never verified state."""
import json
import math

from .result_view import reference_label, previous_handles, retained_output_id

LABEL = 'Tool observations (unverified; earlier results may be stale): '
SOURCE_LABEL = 'Observed JSON fields (file is the source; value is the observed scalar. If absent, read the file): '
SOURCE_BUDGET = 420
_MISSING = object()
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


def _prior_json(content, label, budget):
    if not isinstance(content, str) or not content.startswith('[compacted '):
        return None
    for line in content.splitlines():
        if line.startswith(label) and len(line) - len(label) <= budget:
            try:
                return json.loads(line[len(label):])
            except (ValueError, RecursionError):
                return None


def _previous(content):
    rows = _prior_json(content, LABEL, 900)
    if (isinstance(rows, list) and len(rows) <= 6 and all(
            isinstance(r, dict) and set(r) <= {'tool', 'target', 'output'}
            and {'tool', 'output'} <= set(r)
            and all(isinstance(v, str) for v in r.values())
            and len(r['tool']) <= 64 and len(r.get('target', '')) <= 96
            and len(r['output']) <= 233 for r in rows)):
        return rows
    return []


def excerpt_messages(head, readers=()):
    calls, rows = {}, []
    refs = {}
    field_sources = {}

    def index_field(key, path, value=_MISSING):
        if (not isinstance(path, str) or not 0 < len(path) <= 140
                or not isinstance(key, str) or not 0 < len(key) <= 64):
            return
        previous = field_sources.get(key)
        if previous is not None:
            # Re-reading one file cannot disambiguate a field also seen elsewhere.
            old_value = previous.get('value', _MISSING)
            if previous['file'] != path or type(old_value) is not type(value) or old_value != value:
                previous.pop('value', None)
            return
        row = {'file': path}
        if (type(value) in (str, int, bool, type(None))
                or type(value) is float and math.isfinite(value)):
            if len(json.dumps(value, ensure_ascii=False)) <= 64:
                row['value'] = value
        field_sources[key] = row
        if len(json.dumps(field_sources, ensure_ascii=False)) > SOURCE_BUDGET:
            row.pop('value', None)
            if len(json.dumps(field_sources, ensure_ascii=False)) > SOURCE_BUDGET:
                del field_sources[key]

    def retain(handle):
        if handle:
            refs.pop(handle, None)
            refs[handle] = None
            if len(refs) > 4:
                del refs[next(iter(refs))]

    def remember(identifier, name, args):
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError, RecursionError):
                args = {}
        target = next((args[k] for k in ('path', 'file')
                       if isinstance(args, dict) and isinstance(args.get(k), str)), '')
        requested = args.get('id') if isinstance(args, dict) and name in ('artifact_read', 'artifact_grep') else None
        if identifier:
            calls[identifier] = (str(name or 'tool')[:64], target, requested)

    def observe(identifier, output, name=None, *, failed=False):
        tool, target, requested = calls.pop(identifier, (str(name or 'tool')[:64], '', None))
        if not failed:
            retain(retained_output_id(output, requested))
            if tool in WRITES and target:
                for key in [key for key, entry in field_sources.items() if entry['file'] == target]:
                    del field_sources[key]
            if tool == 'read_file' and target and isinstance(output, str) and output.lstrip().startswith('{'):
                try:
                    document = json.loads(output)
                    if isinstance(document, dict):
                        for key, value in document.items():
                            index_field(key, target, value)
                except (ValueError, RecursionError):
                    pass
        row = {'tool': tool, 'output': _excerpt(output)}
        if target:
            row['target'] = _excerpt(target, 93)
        _add(rows, row)

    for message in head:
        role = message.get('role')
        content = message.get('content', '')
        blocks = content if isinstance(content, list) else []
        if role == 'user':
            for row in _previous(content):
                _add(rows, dict(row))
            for handle in previous_handles(content):
                retain(handle)
            sources = _prior_json(content, SOURCE_LABEL, SOURCE_BUDGET)
            if isinstance(sources, dict):
                for key, entry in sources.items():
                    if isinstance(entry, dict):
                        index_field(key, entry.get('file'), entry.get('value', _MISSING))
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    observe(block.get('tool_use_id'), block.get('content', ''), failed=block.get('is_error', False))
        elif role == 'assistant':
            for call in message.get('tool_calls') or []:
                function = call.get('function') or call
                remember(call.get('id'), function.get('name'), function.get('arguments', function.get('args', {})))
            for block in blocks:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    remember(block.get('id'), block.get('name'), block.get('input', {}))
        elif role == 'tool':
            observe(message.get('tool_call_id'), content, message.get('name'),
                    failed=isinstance(content, str) and content.startswith('[error] '))
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
    suffix = '\n' + reference_label(readers) + ', '.join(refs) if refs else ''
    if field_sources:
        suffix += '\n' + SOURCE_LABEL + json.dumps(field_sources, ensure_ascii=False)
    return f'[compacted {len(head)} earlier messages; unverified excerpts]\n{flat}' + suffix
