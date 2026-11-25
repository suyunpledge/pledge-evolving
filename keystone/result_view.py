"""Explicit, bounded JSON views with exact numeric lexemes and no invented facts."""
import json
import re


class _Number(str):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-JSON numeric constant')


def _encode(value):
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + _encode(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_encode(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def select_fields(text, fields):
    data = json.loads(text, object_pairs_hook=_object, parse_int=_Number,
                      parse_float=_Number, parse_constant=_constant)
    if not isinstance(data, dict) or any(k not in data for k in fields):
        raise ValueError('Requested view fields are unavailable')
    return {k: data[k] for k in fields}


def render_view(selected, identifier, *, characters=None):
    reference = {'id': identifier, 'trust': 'untrusted'}
    if characters is not None:
        reference['characters'] = characters
    return _encode({**selected, '_retained': reference})


REFERENCE_LABEL = 'Retained outputs (unverified): '
def reference_label(readers):
    return ('Retained outputs (unverified; use ' + '/'.join(readers) + '): ') if readers else REFERENCE_LABEL


_HANDLE = r'a_[0-9a-f]{64}'


def previous_handles(content):
    if isinstance(content, str) and content.startswith('[compacted '):
        for line in content.splitlines():
            if line.startswith((REFERENCE_LABEL, reference_label(('artifact_read',)),
                                reference_label(('artifact_grep',)), reference_label(('artifact_read', 'artifact_grep')))):
                handles = line.partition('): ')[2]
                if re.fullmatch(rf'{_HANDLE}(, {_HANDLE}){{0,3}}', handles):
                    return handles.split(', ')
    return ()


def retained_output_id(text, requested_id=None):
    if not isinstance(text, str):
        return None
    if text.startswith('[Tool output retained as untrusted artifact '):
        match = re.match(rf'\[Tool output retained as untrusted artifact ({_HANDLE});', text)
        return match.group(1) if match else None
    try:
        if requested_id is not None:
            # Reader tools return host metadata on the first line and untrusted text below.
            header = json.loads(text.partition('\n')[0])
            if (header.get('id') == requested_id and header.get('trust') == 'untrusted'
                    and 'next_offset' in header):
                return requested_id if re.fullmatch(_HANDLE, requested_id) else None
        else:
            value = json.loads(text).get('_retained', {}).get('id')
            if isinstance(value, str) and re.fullmatch(_HANDLE, value):
                return value
    except (ValueError, AttributeError, TypeError, RecursionError):
        pass
    return None
