"""Bounded, in-memory text and unit tools."""
import base64
import hashlib
import json
import math
import re
from urllib.parse import quote, unquote


TEXT_ACTIONS = (
    'base64_encode', 'base64_decode', 'url_encode', 'url_decode', 'sha256', 'md5',
    'json_format', 'json_query', 'case_convert', 'count',
)
_TEXT_LIMIT = 64 * 1024
_PATH_PART = re.compile(r'(?:^|\.)([^.\[\]]+)|\[([0-9]{1,8})\]')


def _json_value(text, path):
    value = json.loads(text)
    if path is not None:
        if not isinstance(path, str) or not 0 < len(path) <= 512:
            raise ValueError('Provide a JSON path such as items[0].name.')
        offset = 0
        while offset < len(path):
            part = _PATH_PART.match(path, offset)
            if part is None:
                raise ValueError('Invalid JSON path.')
            key, index = part.groups()
            if key is not None and isinstance(value, dict) and key in value:
                value = value[key]
            elif index is not None and isinstance(value, list) and int(index) < len(value):
                value = value[int(index)]
            else:
                raise ValueError('JSON path was not found.')
            offset = part.end()
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError('JSON nesting exceeds 32 levels.')
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def transform_text(action, text, *, mode=None, path=None):
    if not isinstance(text, str) or len(text) > _TEXT_LIMIT:
        raise ValueError('Provide UTF-8 text up to 64 KiB.')
    data = text.encode('utf-8')
    if len(data) > _TEXT_LIMIT:
        raise ValueError('Input text exceeds 64 KiB.')
    if action == 'base64_encode':
        result = base64.b64encode(data).decode('ascii')
    elif action == 'base64_decode':
        result = base64.b64decode(''.join(text.split()), validate=True).decode('utf-8')
    elif action == 'url_encode':
        result = quote(text, safe="!'()*")
    elif action == 'url_decode':
        if re.search(r'%(?![a-fA-F0-9]{2})', text):
            raise ValueError('Invalid URL encoding.')
        result = unquote(text, errors='strict')
    elif action == 'sha256':
        result = hashlib.sha256(data).hexdigest()
    elif action == 'md5':
        result = hashlib.md5(data, usedforsecurity=False).hexdigest()
    elif action in ('json_format', 'json_query'):
        result = _json_value(text, (path or '') if action == 'json_query' else None)
    elif action == 'case_convert':
        convert = {'upper': str.upper, 'lower': str.lower, 'title': str.title}
        if mode not in convert:
            raise ValueError('Case mode must be upper, lower, or title.')
        result = convert[mode](text)
    elif action == 'count':
        counts = {'chars': len(text), 'words': len(text.split()), 'lines': text.count('\n') + 1 if text else 0}
        if mode in (None, 'all'):
            return json.dumps(counts)
        if mode not in counts:
            raise ValueError('Count mode must be all, chars, words, or lines.')
        return str(counts[mode])
    else:
        raise ValueError('Unknown text action.')
    if len(result.encode('utf-8')) > 256 * 1024:
        raise ValueError('Output exceeds 256 KiB.')
    return result


_UNIT_GROUPS = (
    {'m': 1, 'km': 1000, 'cm': .01, 'mm': .001, 'mile': 1609.344, 'ft': .3048, 'inch': .0254},
    {'kg': 1, 'g': .001, 'lb': .45359237, 'oz': .028349523125},
    {'B': 1, 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'TB': 1000**4,
     'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4},
    {'s': 1, 'min': 60, 'h': 3600, 'day': 86400},
)
_TEMPERATURE = {'C', 'F', 'K'}
UNITS = tuple(unit for group in _UNIT_GROUPS for unit in group) + ('C', 'F', 'K')


def convert_unit(value, source, target):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Provide a finite number.')
    if not isinstance(source, str) or not isinstance(target, str):
        raise ValueError('Provide source and target units.')
    if source in _TEMPERATURE and target in _TEMPERATURE:
        if source == target:
            return value
        celsius = value if source == 'C' else (value - 32) * (5 / 9) if source == 'F' else value - 273.15
        result = celsius if target == 'C' else celsius * (9 / 5) + 32 if target == 'F' else celsius + 273.15
    else:
        group = next((group for group in _UNIT_GROUPS if source in group and target in group), None)
        if group is None:
            raise ValueError('Choose two supported units from the same category.')
        result = value * (group[source] / group[target])
    if not math.isfinite(result):
        raise ValueError('Conversion result is outside the numeric range.')
    return result
