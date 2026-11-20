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


REFERENCE_LABEL = 'Retained outputs (unverified; use artifact_read/artifact_grep): '


def retained_handles(messages):
    """Carry at most four actual output references across successive excerpts."""
    found = {}
    for message in messages:
        content = message.get('content', '')
        blocks = content if isinstance(content, list) else []
        texts = ([(content, False)] if message.get('role') == 'tool' else [])
        texts += [(b.get('content', ''), False) for b in blocks
                  if isinstance(b, dict) and b.get('type') == 'tool_result']
        if isinstance(content, str) and content.startswith('[compacted '):
            texts += [(line[len(REFERENCE_LABEL):], True) for line in content.splitlines()
                      if line.startswith(REFERENCE_LABEL)]
        for text, previous_reference in texts:
            if not isinstance(text, str):
                continue
            # Only reference syntax emitted by retention or a previous excerpt.
            candidates = []
            if text.startswith('[Tool output retained as untrusted artifact '):
                candidates = re.findall(r'\ba_[0-9a-f]{64}\b', text.split('\n', 1)[0])
            elif previous_reference and re.fullmatch(r'a_[0-9a-f]{64}(, a_[0-9a-f]{64})*', text):
                candidates = text.split(', ')
            else:
                try:
                    value = json.loads(text).get('_retained', {}).get('id')
                    if isinstance(value, str) and re.fullmatch(r'a_[0-9a-f]{64}', value):
                        candidates = [value]
                except (ValueError, AttributeError, TypeError, RecursionError):
                    pass
            for value in candidates:
                found.pop(value, None)
                found[value] = None
                if len(found) > 4:
                    del found[next(iter(found))]
    return list(found)
