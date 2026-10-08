"""Structured, optimistic, atomic configuration edits in the trusted layer."""
from __future__ import annotations
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import weakref

from .policy import Decision
from .secrets import REF, assert_public_path, secret_field, _restore_config, read_public_bytes

_LOCK = threading.RLock()
_VIEWS = weakref.WeakKeyDictionary()
MAX_BYTES = 1024 * 1024
_ENV = re.compile(r'^(?:export\s+)?(?P<key>[A-Za-z_][\w]*)\s*=\s*'
                  r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\r\n]*)', re.M)


@contextmanager
def _process_lock(path):
    """Serialize cooperating Forge processes across inspect/compare/replace."""
    directory = Path(tempfile.gettempdir()) / 'forge-config-locks'
    directory.mkdir(exist_ok=True)
    name = hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()
    with (directory / name).open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Configuration is busy; retry after the current edit') from None
                time.sleep(0.02)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _env_value(raw):
    if raw[:1] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw[:1] == "'":
        return raw[1:-1]
    return re.split(r'\s+#', raw, maxsplit=1)[0].rstrip()


def _parse(path, text):
    kind = path.suffix.lower()
    if path.name == '.env' or path.name.startswith('.env.') or kind == '.env':
        pairs = list(_ENV.finditer(text))
        if len({m['key'] for m in pairs}) != len(pairs):
            raise ValueError('Duplicate .env keys are ambiguous')
        value = {m['key']: _env_value(m['value']) for m in pairs}
        def dump(data):
            out, seen, offset = [], set(), 0
            for m in pairs:
                out.append(text[offset:m.start()])
                key = m['key']
                seen.add(key)
                if key in data:
                    if data[key] == value[key]:
                        out.append(m[0])
                    else:
                        if not isinstance(data[key], str):
                            raise ValueError('.env values must be strings')
                        comment = re.search(r'\s+#.*$', m['value']) if m['value'][:1] not in {'"', "'"} else None
                        out.append(text[m.start():m.start('value')] + json.dumps(data[key], ensure_ascii=False)
                                   + (comment[0] if comment else ''))
                offset = m.end()
            out.append(text[offset:])
            result = ''.join(out)
            for key in data.keys() - seen:
                if not re.fullmatch(r'[A-Za-z_][\w]*', key) or not isinstance(data[key], str):
                    raise ValueError('Invalid .env key/value')
                result += ('\n' if not result.endswith('\n') else '') + key + '=' + json.dumps(data[key], ensure_ascii=False) + '\n'
            return result
        return value, dump
    if kind == '.json':
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError('Duplicate JSON keys are ambiguous')
                out[key] = value
            return out
        value = json.loads(text, object_pairs_hook=unique)
        return value, lambda data: json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if kind in {'.yaml', '.yml'}:
        try:
            import yaml
        except ImportError:
            raise ValueError('YAML editing requires the optional PyYAML dependency') from None
        # Aliases and custom tags permit surprising shared writes; reject them.
        for token in yaml.scan(text):
            if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken)):
                raise ValueError('YAML aliases, anchors and custom tags cannot be edited safely')
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node, deep=False):
            out = {}
            for key, item in node.value:
                key = loader.construct_object(key, deep=deep)
                if key in out:
                    raise ValueError('Duplicate YAML keys are ambiguous')
                out[key] = loader.construct_object(item, deep=deep)
            return out
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        return yaml.load(text, Loader=UniqueLoader), lambda data: yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    if kind == '.toml':
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        value = tomllib.loads(text)
        return value, _toml_dump
    raise ValueError('edit_config supports JSON, YAML, TOML and .env files')


def _toml_dump(data):
    """Conservative TOML serializer: JSON-like config only, no executable loads."""
    def key(k):
        return json.dumps(str(k), ensure_ascii=False)
    def scalar(v):
        if isinstance(v, str): return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool): return 'true' if v else 'false'
        if isinstance(v, (int, float)): return json.dumps(v, allow_nan=False)
        if isinstance(v, list): return '[' + ', '.join(scalar(x) for x in v) + ']'
        if isinstance(v, dict): return '{ ' + ', '.join(key(k) + ' = ' + scalar(x) for k, x in v.items()) + ' }'
        import datetime
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)): return v.isoformat()
        raise ValueError('Unsupported TOML value')
    if not isinstance(data, dict):
        raise ValueError('TOML root must be a table')
    return ''.join(key(k) + ' = ' + scalar(v) + '\n' for k, v in data.items())


def _leaves(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items(): yield from _leaves(v, (*path, str(k)))
    elif isinstance(value, list):
        for k, v in enumerate(value): yield from _leaves(v, (*path, str(k)))
    else:
        yield path, value


def _pointer(raw):
    if not isinstance(raw, str) or not raw.startswith('/'):
        raise ValueError('Patch path must be a JSON Pointer below the configuration root')
    if re.search(r'~(?![01])', raw):
        raise ValueError('Invalid JSON Pointer escape')
    return tuple(p.replace('~1', '/').replace('~0', '~') for p in raw[1:].split('/'))


def _patch(data, patches):
    result = copy.deepcopy(data)
    if not isinstance(patches, list) or not 1 <= len(patches) <= 100:
        raise ValueError('patch must contain 1–100 operations')
    for patch in patches:
        if not isinstance(patch, dict) or patch.get('op') not in {'add', 'replace', 'remove', 'test'}:
            raise ValueError('Supported patch operations: add, replace, remove, test')
        parts = _pointer(patch.get('path'))
        parent = result
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        part = parts[-1]
        is_list = isinstance(parent, list)
        key = (len(parent) if part == '-' and patch['op'] == 'add' else int(part)) if is_list else part
        if is_list and (key < 0 or key > len(parent)):
            raise ValueError('Array index is out of bounds')
        if not isinstance(parent, (dict, list)):
            raise ValueError('Patch parent must be an object or array')
        if patch['op'] == 'test':
            if parent[key] != patch.get('value'):
                raise ValueError('Patch test failed')
        elif patch['op'] == 'remove':
            del parent[key]
        else:
            if 'value' not in patch:
                raise ValueError('Patch value is missing')
            if patch['op'] == 'replace' and (not is_list and key not in parent or is_list and key == len(parent)):
                raise ValueError('Replace target is missing')
            if is_list and patch['op'] == 'add': parent.insert(key, patch['value'])
            else: parent[key] = patch['value']
    return result


def _protected_fields(value, path=(), vendor=False):
    if isinstance(value, dict):
        vendor = vendor or any(k in value for k in ('vendor', 'base_url', 'baseURL', 'provider'))
        for key, item in value.items():
            next_path = (*path, str(key))
            if secret_field(str(key), vendor=vendor):
                yield next_path, item
            else:
                yield from _protected_fields(item, next_path, vendor)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _protected_fields(item, (*path, str(index)), vendor)


def sensitive_config(path: Path, text: str) -> bool:
    """Indirection/empty credential fields also require the structured editor."""
    try:
        data, _ = _parse(path, text)
    except ValueError:
        from .secrets import _ASSIGN
        return any(secret_field(m['key']) for m in _ASSIGN.finditer(text))
    return bool(list(_protected_fields(data)) or list(_control_fields(data)))


def _control_fields(value, path=()):
    """Host execution/authorization configuration is not agent-editable."""
    names = {'policy', 'sandbox', 'permissions', 'capabilities', 'grants', 'secretisolation',
             'toolregistry', 'pluginruntime', 'modules', 'entrypoint', 'executescode', 'plugins'}
    if isinstance(value, dict):
        # Forge layers are rows, e.g. {id: policy, config: {allow: [...]}}.
        # Protect the whole authority row, including disabled/id/name/inject;
        # checking for a dictionary key named "policy" misses the real format.
        row_id = re.sub(r'[^a-z0-9]', '', str(value.get('id', '')).lower())
        row_kind = str(value.get('name', '')).split(':', 1)[0].lower()
        if 'id' in value and (row_id in names | {'tools'} or row_kind in names | {'tools'}):
            yield path, value
            return
        for key, item in value.items():
            next_path = (*path, str(key))
            if key == '$expr' or re.sub(r'[^a-z0-9]', '', str(key).lower()) in names:
                yield next_path, item
            else:
                yield from _control_fields(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _control_fields(item, (*path, str(index)))


def _validate(original, candidate, *, scope=None, target='', revision=''):
    if dict(_control_fields(original)) != dict(_control_fields(candidate)):
        raise PermissionError('Forge execution/permission controls must be changed by the human host')
    def permitted(path, capability, value):
        if scope is None: return False
        pointer = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in path)
        key = (target, revision, pointer, capability)
        return key in scope._config_grants and scope._config_grants[key] == value
    before, after = dict(_protected_fields(original)), dict(_protected_fields(candidate))
    def destinations(node, path=()):
        if isinstance(node, dict):
            if list(_protected_fields(node)):
                names = {'baseurl', 'apibase', 'endpoint', 'url', 'host', 'port', 'vendor', 'provider',
                         'proxy', 'httpproxy', 'httpsproxy', 'verifyssl', 'verifytls', 'insecure', 'headers'}
                for key in node:
                    if re.sub(r'[^a-z0-9]', '', str(key).lower()) in names:
                        yield (*path, str(key)), node[key]
            for key, item in node.items():
                yield from destinations(item, (*path, str(key)))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                yield from destinations(item, (*path, str(index)))
    old_dest, new_dest = dict(destinations(original)), dict(destinations(candidate))
    for path in old_dest.keys() | new_dest.keys():
        if old_dest.get(path) != new_dest.get(path) and not permitted(
                path, 'secret.destination', json.dumps(new_dest.get(path), sort_keys=True)):
            raise PermissionError('Changing a credential destination requires a bound human secret.use grant')
    allowed_fields = set(before) | set(after)
    for path in allowed_fields:
        if path in before and path in after and before[path] == after[path]: continue
        if path not in after:
            if not permitted(path, 'secret.delete', None):
                raise PermissionError('Deleting a secret requires an explicit field grant')
        else:
            value = after[path]
            if not isinstance(value, str) or not REF.fullmatch(value):
                raise PermissionError('Secret changes must use an exact approved reference')
            cap = 'secret.replace' if path in before else 'secret.create'
            if not permitted(path, cap, value) and not permitted(path, 'secret.use', value):
                raise PermissionError('Secret fields and their environment references require a human secret capability grant')
    old, new = dict(_leaves(original)), dict(_leaves(candidate))
    for path, value in old.items():
        if isinstance(value, str) and ('SECRET_REF:' in value):
            if new.get(path) != value:
                if path not in allowed_fields or (new.get(path) is not None and
                        not (permitted(path, 'secret.replace', new.get(path)) or permitted(path, 'secret.use', new.get(path)))):
                    if not permitted(path, 'secret.delete', None):
                        raise PermissionError('Protected secret cannot be removed, replaced or swapped; use the human credential editor')
    for path, value in new.items():
        if isinstance(value, str) and ('SECRET_REF' in value or '{{SECRET' in value):
            if old.get(path) != value or not REF.search(value):
                if path not in allowed_fields or not REF.fullmatch(value) or not any(
                        permitted(path, cap, value) for cap in ('secret.use', 'secret.create', 'secret.replace')):
                    raise PermissionError('Forged, moved or malformed SecretRef')
        if path and secret_field(path[-1]) and old.get(path) != value:
            if not any(permitted(path, cap, value) for cap in ('secret.use', 'secret.create', 'secret.replace')):
                raise PermissionError('Creating/replacing/deleting credentials requires a human secret capability grant')
    # Deleting an empty protected field also requires the secret delete gate.
    for path in old.keys() - new.keys():
        if path and secret_field(path[-1]):
            if not permitted(path, 'secret.delete', None):
                raise PermissionError('Deleting a secret field requires a human secret capability grant')


def edit_config(args, ctx):
    from .tools import ToolResult
    path = ctx.policy.abs_path(str(args.get('path', ''))).resolve()
    assert_public_path(path)
    if not ctx.policy.allows_read(path):
        raise PermissionError('Configuration read denied by sandbox')
    if ctx.policy.evaluate('secret.reference') is not Decision.ALLOW:
        raise PermissionError('Secret reference capability denied')
    with _LOCK, _process_lock(path):
        raw = read_public_bytes(path, limit=MAX_BYTES)
        text = raw.decode('utf-8-sig')
        original, dump = _parse(path, text)
        safe = ctx.secret_scope.protect(original)
        digest = hashlib.sha256(raw).digest()
        views = _VIEWS.setdefault(ctx.secret_scope, {})
        patches = args.get('patch')
        if patches is not None:
            if not ctx.policy.allows_write(path):
                raise PermissionError('Configuration write denied by sandbox')
            view = views.get(path)
            if not view or args.get('revision') != view[0] or digest != view[1]:
                raise ValueError('Configuration changed or revision expired; inspect edit_config again')
            candidate = _patch(safe, patches)
            _validate(view[2], candidate, scope=ctx.secret_scope, target=str(path), revision=view[0])
            # Never accept literals discovered as secrets in ordinary fields either.
            if ctx.secret_scope.protect(candidate) != candidate:
                raise PermissionError('Raw secret values cannot be supplied in an agent patch')
            restored = _restore_config(ctx.secret_scope, candidate, str(path))
            rendered = dump(restored)
            reparsed, _ = _parse(path, rendered)
            if reparsed != restored:
                raise ValueError('Configuration cannot be serialized without changing its meaning')
            encoded = (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + rendered.encode('utf-8')
            if read_public_bytes(path, limit=MAX_BYTES) != raw:
                raise ValueError('Configuration changed during validation')
            handle, temp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(handle, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temp, path.stat().st_mode & 0o777)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)
            safe = candidate
            digest = hashlib.sha256(encoded).digest()
            ctx.secret_scope._config_grants = {k: v for k, v in ctx.secret_scope._config_grants.items()
                                              if k[:2] != (str(path), view[0])}
        revision = os.urandom(16).hex()
        views[path] = (revision, digest, copy.deepcopy(safe))
        count = sum(len(REF.findall(v)) for _, v in _leaves(safe) if isinstance(v, str))
        ctx.fire(type='secret_protection', action='config.edit' if patches is not None else 'config.inspect',
                 protected=count, path=str(path))
        return ToolResult(True, content=json.dumps(safe, ensure_ascii=False, indent=2, default=str),
                          meta={'revision': revision, 'protected_secrets': count,
                                'changed': patches is not None})
