"""Trusted secret virtualization; never expose a resolver as an agent tool.

This is a capability boundary inside Forge, not an OS sandbox for arbitrary
Python/native code running as the same user. Protected agents use mediated
tools only. The store is ephemeral; persisted sessions contain references only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import base64
import json
import logging
import os
from pathlib import Path
import re
import threading
from urllib.parse import quote, urlsplit

MARKER = '[REDACTED_SECRET]'
REF = re.compile(r'\{\{SECRET_REF:([A-F0-9]{32})\}\}')
_LOCK = threading.RLock()
_KNOWN: set[str] = set()
_NUMERIC_VALUES: set[tuple[type, object]] = set()
_KNOWN_BYTES = 0
_PATHS: set[Path] = set()
_IDENTITIES: set[tuple[int, int]] = set()
_KEYS = {'apikey', 'accesskey', 'secretkey', 'secretaccesskey', 'accesskeysecret',
         'accesstoken', 'refreshtoken', 'authtoken', 'token', 'password', 'passwd',
         'pwd', 'authorization', 'bearer', 'privatekey', 'cookie', 'cookies',
         'sessiontoken', 'sessionid', 'clientsecret', 'signingkey', 'credential',
         'credentials', 'gatewaytoken', 'secret', 'xapikey'}
_VENDORS = ('openai', 'anthropic', 'deepseek', 'qwen', 'dashscope', 'mimo',
            'gemini', 'google', 'azure', 'aws', 'github', 'huggingface', 'forge')
_FORMAT = re.compile(r'(?<![\w-])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|'
                     r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
                     r'AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')
_ASSIGN = re.compile(
    r'(?<![\w.-])(?P<head>["\']?(?P<key>[\w.-]{1,128})["\']?\s*[:=]\s*)')
_VALUE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\r\n,;}{\[\]"\']+')
_COOKIE_VALUE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\r\n"\']+')
_AUTH = re.compile(r'\bBearer\s+([^\s"\'<>;,]+)', re.I)
_URL_PASSWORD = re.compile(r'(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://[^\s/@:]+:)([^\s/@]+)(@)', re.I)


def secret_field(name: str, *, vendor: bool = False) -> bool:
    key = re.sub(r'[^a-z0-9]', '', str(name).lower())
    return (key in _KEYS or any(key.endswith(t) for t in
            ('apikey', 'password', 'passwd', 'accesstoken', 'refreshtoken',
             'clientsecret', 'privatekey', 'sessiontoken', 'accesskeysecret'))
            or (vendor and key in {'key', 'auth', 'secret'})
            or any(key.startswith(v) and key.endswith(('key', 'token', 'secret')) for v in _VENDORS))


def _variants(value: str) -> set[str]:
    values = {value}
    bearer = re.fullmatch(r'Bearer\s+(\S+)', value, re.I)
    if bearer:
        values.add(bearer[1])
    # A declared Cookie/Authorization string can contain multiple credentials.
    # Also remember their scalar values, so a later echo without its header is safe.
    for part in value.split(';'):
        cookie = re.fullmatch(r'\s*[A-Za-z_][\w-]{0,64}=([^\s;]+)\s*', part)
        if cookie:
            values.add(cookie[1])
    return {variant for item in values for variant in
            (item, json.dumps(item, ensure_ascii=False)[1:-1],
             json.dumps(item, ensure_ascii=True)[1:-1], quote(item, safe=''),
             base64.b64encode(item.encode()).decode())}


def _remember(value: str) -> None:
    global _KNOWN_BYTES
    if value:
        if len(value.encode('utf-8')) > 64 * 1024:
            raise ValueError('Secret exceeds the 64 KiB value limit')
        variants = _variants(value)
        with _LOCK:
            added = variants - _KNOWN
            size = sum(len(v.encode('utf-8')) for v in added)
            if _KNOWN_BYTES + size > 16 * 1024 * 1024:
                raise ValueError('Secret redaction capacity exceeded')
            _KNOWN.update(added)
            _KNOWN_BYTES += size


def _known_text(text: str, replace) -> str:
    with _LOCK:
        values = sorted(_KNOWN, key=len, reverse=True)
    # Protect existing opaque references from short-value substitutions.
    parts = re.split(r'(\{\{SECRET_REF:[A-F0-9]{32}\}\}|\[REDACTED_SECRET\])', text)
    # A forged syntactically valid ID can itself be a 32-hex credential.
    # Public reference syntax never exempts an exact known credential value.
    for i in range(1, len(parts), 2):
        ref = REF.fullmatch(parts[i])
        if ref and ref[1] in values:
            parts[i] = replace(ref[1])
    present = [v for v in values if v in text]
    if present:
        pattern = re.compile('|'.join((r'(?<!\w)' + re.escape(v) + r'(?!\w)')
                                      if len(v) < 8 else re.escape(v) for v in present))
        for i in range(0, len(parts), 2):
            parts[i] = pattern.sub(lambda m: replace(m[0]), parts[i])
    return ''.join(parts)


class SecretStore(ABC):
    """Local backend contract; intentionally no public get/list/export API."""
    @abstractmethod
    def _put(self, scope: str, value: object) -> str: ...

    @abstractmethod
    def _read(self, grant: '_UseGrant', *, target: str, operation: str) -> object: ...

    @abstractmethod
    def _revoke(self, scope: str) -> None: ...

    @abstractmethod
    def _owns(self, scope: str, ref: str) -> bool: ...


class MemorySecretStore(SecretStore):
    def __init__(self):
        self.__items = {}
        self.__lock = threading.RLock()

    def _put(self, scope, value):
        with self.__lock:
            if len(self.__items) >= 4096:
                raise ValueError('Secret reference capacity exceeded')
            ref = '{{SECRET_REF:' + os.urandom(16).hex().upper() + '}}'
            _remember(value if isinstance(value, str) else json.dumps(value, allow_nan=False))
            if isinstance(value, (int, float, bool)):
                with _LOCK:
                    _NUMERIC_VALUES.add((type(value), value))
            self.__items[ref] = (scope, value)
            return ref

    def _read(self, grant, *, target, operation):
        if (not isinstance(grant, _UseGrant) or grant._seal is not _SEAL
                or grant.target != target or grant.operation != operation):
            raise PermissionError('Secret capability denied')
        with self.__lock:
            item = self.__items.get(grant.ref)
            if item is None or item[0] != grant.scope:
                raise PermissionError('Secret reference expired or belongs to another session')
            return item[1]

    def _revoke(self, scope):
        with self.__lock:
            self.__items = {k: v for k, v in self.__items.items() if v[0] != scope}

    def _owns(self, scope, ref):
        with self.__lock:
            return ref in self.__items and self.__items[ref][0] == scope


_SEAL = object()


class _UseGrant:
    __slots__ = ('scope', 'ref', 'target', 'operation', '_seal')

    def __init__(self, scope, ref, target, operation, seal):
        self.scope, self.ref, self.target, self.operation = scope, ref, target, operation
        self._seal = seal


class SecretScope:
    """Session facade: may create/reference/protect, never resolve or enumerate."""
    def __init__(self, store: SecretStore | None = None):
        self._store = store or MemorySecretStore()
        self._id = os.urandom(16).hex()
        self.__refs = {}
        self.__salt = os.urandom(32)
        self.__lock = threading.RLock()
        self.__closed = False
        self._config_grants = {}

    def reference(self, value):
        if value is None or value == '' or isinstance(value, str) and (REF.fullmatch(value) or value == MARKER):
            return value
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError('Unsupported secret value type')
        with self.__lock:
            if self.__closed:
                raise PermissionError('Secret session is closed')
            import hashlib, hmac
            canonical = json.dumps([type(value).__name__, value], ensure_ascii=False, allow_nan=False)
            lookup = hmac.new(self.__salt, canonical.encode('utf-8'), hashlib.sha256).digest()
            if lookup not in self.__refs:
                self.__refs[lookup] = self._store._put(self._id, value)
            return self.__refs[lookup]

    def protect_text(self, text: str) -> str:
        text = str(text)
        if len(text) > 8 * 1024 * 1024:
            raise ValueError('Content exceeds the 8 MiB secret inspection limit')
        # Inspect JSON semantically so punctuation/numeric credentials cannot
        # replace protocol delimiters or leave unquoted references behind.
        if text.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(text)
            except ValueError:
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    protected = self.protect(parsed)
                    return json.dumps(protected, ensure_ascii=False, default=str) if protected != parsed else text
        text = _known_text(text, self.reference)
        # Deterministic scanning also protects unterminated private-key blocks.
        # A regex searching for END repeatedly is quadratic on hostile inputs.
        start_pattern = re.compile(r'-----BEGIN ([A-Z ]*PRIVATE KEY)-----')
        pieces, offset = [], 0
        while True:
            begin = start_pattern.search(text, offset)
            if not begin: break
            end_tag = '-----END ' + begin[1] + '-----'
            end = text.find(end_tag, begin.end())
            end = len(text) if end < 0 else end + len(end_tag)
            pieces.extend((text[offset:begin.start()], self.reference(text[begin.start():end])))
            offset = end
        if pieces:
            text = ''.join(pieces) + text[offset:]
        pieces, offset = [], 0
        vendor = any(re.sub(r'[^a-z0-9]', '', m['key'].lower()) in
                     {'vendor', 'baseurl', 'provider'} for m in _ASSIGN.finditer(text))
        for m in _ASSIGN.finditer(text):
            if m.start() < offset or not secret_field(m['key'], vendor=vendor):
                continue
            parser = _COOKIE_VALUE if m['key'].lower() in {'cookie', 'cookies'} else _VALUE
            match = parser.match(text, m.end())
            if not match: continue
            raw = match[0].strip()
            # Preserve quoting. A dollar prefix or embedded reference syntax is
            # not proof of environment indirection: passwords can contain both.
            quote_char = raw[:1] if raw[:1] in {'"', "'"} else ''
            value = raw[1:-1] if quote_char else raw.split(' #', 1)[0].strip()
            if not quote_char and (value in {'null', 'None', 'true', 'false'} or value.isdecimal()):
                continue  # Structural JSON/Python values are not secret strings.
            if not value or REF.fullmatch(value) or value == MARKER:
                continue
            if quote_char == '"':
                try:
                    value = json.loads(raw)
                except ValueError:
                    pass
            if m['key'] == 'authorization' and value in {'allow', 'deny', 'ask'}:
                continue  # Forge's Policy decision, not an HTTP credential
            ref = self.reference(value)
            pieces.extend((text[offset:m.start()], m['head'] + quote_char + ref + quote_char))
            offset = match.end()
        if pieces:
            text = ''.join(pieces) + text[offset:]
        text = _AUTH.sub(lambda m: 'Bearer ' + self.reference(m[1]), text)
        text = _URL_PASSWORD.sub(lambda m: m[1] + self.reference(m[2]) + m[3], text)
        text = _FORMAT.sub(lambda m: self.reference(m[0]), text)
        return _known_text(text, self.reference)

    def protect(self, value, *, vendor: bool = False, _depth: int = 0, _container_secret=False):
        if _depth == 0 and isinstance(value, (dict, list, tuple)):
            with _LOCK:
                generation = (_KNOWN_BYTES, len(_NUMERIC_VALUES))
            safe = self.protect(value, vendor=vendor, _depth=1,
                                _container_secret=_container_secret)
            with _LOCK:
                discovered = generation != (_KNOWN_BYTES, len(_NUMERIC_VALUES))
            # A declaration at the end of a document can classify a value that
            # appeared earlier. Revisit only when detection registered values.
            return (self.protect(safe, vendor=vendor, _depth=1,
                                 _container_secret=_container_secret) if discovered else safe)
        if _depth > 64:
            raise ValueError('Secret inspection nesting exceeds 64 levels')
        if isinstance(value, dict):
            is_vendor = vendor or any(k in value for k in ('vendor', 'base_url', 'baseURL', 'provider'))
            out = {}
            for key, item in value.items():
                safe_key = self.protect_text(str(key))
                if key == 'authorization' and isinstance(item, str) and item in {'allow', 'deny', 'ask'}:
                    out[safe_key] = item
                    continue
                secret = secret_field(str(key), vendor=is_vendor)
                secret = secret or (_container_secret and str(key).lower() not in
                                    {'name', 'domain', 'path', 'expires', 'samesite', 'username', 'type'})
                if secret and isinstance(item, (str, int, float, bool)) and item is not None:
                    out[safe_key] = self.reference(item)
                else:
                    out[safe_key] = self.protect(item, vendor=is_vendor, _depth=_depth + 1,
                        _container_secret=_container_secret or secret)
            return out
        if isinstance(value, (list, tuple)):
            return [self.protect(v, vendor=vendor, _depth=_depth + 1,
                                 _container_secret=_container_secret) for v in value]
        if isinstance(value, str):
            return self.reference(value) if _container_secret else self.protect_text(value)
        if isinstance(value, (int, float, bool)):
            with _LOCK:
                known = (type(value), value) in _NUMERIC_VALUES
            if known:
                return self.reference(value)
        return value

    def file_text(self, path: Path) -> str:
        assert_public_path(path)
        # Detect the entire file, not a truncated/range fragment of it.
        text = read_public_bytes(path, limit=8 * 1024 * 1024).decode('utf-8-sig')
        return self.document_text(path, text)

    def document_text(self, path: Path, text: str) -> str:
        """Protect a complete editor buffer using the same file-aware detector."""
        path = Path(path)
        assert_public_path(path)
        if not isinstance(text, str) or len(text.encode('utf-8')) > 8 * 1024 * 1024:
            raise ValueError('Document exceeds the 8 MiB secret inspection limit')
        if path.suffix.lower() in {'.json', '.yaml', '.yml', '.toml', '.env'} or path.name.startswith('.env'):
            from .config_edit import _parse
            value, _ = _parse(path, text)
            safe = self.protect(value)  # register semantic values, including escaped strings
            from .config_edit import _leaves
            protected = dict(_leaves(safe))
            if any(isinstance(item, (int, float, bool)) and protected.get(key) != item
                   for key, item in _leaves(value)):
                return json.dumps(safe, ensure_ascii=False, default=str)
        return self.protect_text(text)

    def close(self):
        with self.__lock:
            self._store._revoke(self._id)
            self.__refs.clear()
            self.__closed = True
            self._config_grants.clear()


def redact(value):
    """Output fence: discovered secrets become markers, never resolvable refs."""
    scope = SecretScope()
    try:
        safe = scope.protect(value)
        # Existing SecretRefs must remain usable in outputs/sessions.
        new_refs = set(scope._SecretScope__refs.values())
        def clean(v):
            if isinstance(v, str):
                for ref in new_refs:
                    v = v.replace(ref, MARKER)
                return _known_text(v, lambda _: MARKER)
            if isinstance(v, dict):
                return {clean(k): clean(item) for k, item in v.items()}
            if isinstance(v, list):
                return [clean(item) for item in v]
            return v
        return clean(safe)
    finally:
        scope.close()


def protect_store_path(path: Path) -> None:
    path = Path(path).resolve()
    with _LOCK:
        _PATHS.add(path)
        try:
            st = path.stat()
            _IDENTITIES.add((st.st_dev, st.st_ino))
        except OSError:
            pass


def assert_public_path(path: Path) -> None:
    path = Path(path).resolve()
    defaults = {Path.home() / '.forge' / 'secrets.json',
                Path(os.environ.get('FORGE_HOME', str(Path.home() / '.forge'))) / 'secrets.json'}
    with _LOCK:
        roots = _PATHS | {p.resolve() for p in defaults}
        if any(path == p or p in path.parents for p in roots):
            raise PermissionError('Secret Store is not accessible to agent tools')
        for p in roots:
            try:
                st = p.stat()
                _IDENTITIES.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        try:
            st = path.stat()
            if (st.st_dev, st.st_ino) in _IDENTITIES:
                raise PermissionError('Secret Store alias is not accessible to agent tools')
        except FileNotFoundError:
            pass


def read_public_bytes(path: Path, *, limit: int) -> bytes:
    """Bound the allocation before decoding/parsing untrusted file content."""
    assert_public_path(path)
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'File exceeds the {limit} byte inspection limit')
    return data


def _grant(scope: SecretScope, ref: str, target: str, operation: str) -> _UseGrant:
    if not REF.fullmatch(ref):
        raise PermissionError('Malformed SecretRef')
    return _UseGrant(scope._id, ref, target, operation, _SEAL)


def authorize_config_secret(scope, policy, *, path, revision, pointer, capability, ref=None):
    """Human/host approval API, deliberately absent from every tool schema.

    Grants bind one capability to one session/file/version/field/reference.
    A policy wildcard or BYPASS cannot mint these grants. No value is returned.
    """
    from .policy import Decision
    if capability not in {'secret.use', 'secret.create', 'secret.replace', 'secret.delete'}:
        raise PermissionError('Invalid secret configuration capability')
    if policy.evaluate(capability) is not Decision.ALLOW:
        raise PermissionError('An explicit human secret capability grant is required')
    if ref is not None and (not REF.fullmatch(ref) or not scope._store._owns(scope._id, ref)):
        raise PermissionError('Reference is not owned by this session')
    if capability != 'secret.delete' and ref is None:
        raise PermissionError('Secret use requires an exact owned reference')
    target = str(policy.abs_path(path).resolve())
    assert_public_path(Path(target))
    scope._config_grants[(target, revision, pointer, capability)] = ref


def authorize_config_destination(scope, policy, *, path, revision, pointer, value):
    """Host approval of a credential destination; never registered as a tool."""
    from .policy import Decision
    if policy.evaluate('secret.use') is not Decision.ALLOW:
        raise PermissionError('An explicit human secret.use grant is required')
    target = str(policy.abs_path(path).resolve())
    assert_public_path(Path(target))
    scope._config_grants[(target, revision, pointer, 'secret.destination')] = json.dumps(value, sort_keys=True)


def _restore_config(scope, value, target):
    """Only called after a trusted patch validator approved the exact tree."""
    if isinstance(value, dict):
        return {k: _restore_config(scope, v, target) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_config(scope, v, target) for v in value]
    if isinstance(value, str):
        if REF.fullmatch(value):
            return scope._store._read(_grant(scope, value, target, 'config.preserve'),
                                      target=target, operation='config.preserve')
        def restore(m):
            grant = _grant(scope, m[0], target, 'config.preserve')
            restored = scope._store._read(grant, target=target, operation='config.preserve')
            if not isinstance(restored, str):
                raise PermissionError('Typed SecretRef cannot be embedded in a string')
            return restored
        return REF.sub(restore, value)
    return value


class VendorCredential:
    """Endpoint-bound authentication holder. No public value getter."""
    def __init__(self, value: str, endpoint: str):
        import hashlib
        self.__scope = SecretScope()
        self.ref = self.__scope.reference(value)
        self.__endpoint = endpoint.rstrip('/')
        self._account_fingerprint = hashlib.sha256(value.encode()).hexdigest() if value else ''

    @classmethod
    def from_reference(cls, scope, policy, *, ref, endpoint):
        """Trusted host binds an owned reference to an approved adapter endpoint.

        No resolution occurs here. The adapter consumes it only in _header().
        This factory is deliberately absent from the LLM's tool surface.
        """
        from .policy import Decision
        if policy.evaluate('secret.use') is not Decision.ALLOW:
            raise PermissionError('Vendor secret use requires an explicit human grant')
        if not REF.fullmatch(ref) or not scope._store._owns(scope._id, ref):
            raise PermissionError('Reference is not owned by this session')
        parts = urlsplit(endpoint)
        if parts.username is not None or parts.fragment or not parts.netloc:
            raise PermissionError('Invalid credential destination')
        out = cls.__new__(cls)
        out.__scope, out.ref, out.__endpoint = scope, ref, endpoint.rstrip('/')
        import hashlib
        out._account_fingerprint = hashlib.sha256((scope._id + ref).encode()).hexdigest()
        return out

    def __repr__(self):
        return 'VendorCredential(Protected)'

    def _header(self, url: str, *, wire: str):
        actual, allowed = urlsplit(url), urlsplit(self.__endpoint)
        if (actual.scheme, actual.netloc) != (allowed.scheme, allowed.netloc):
            raise PermissionError('Secret use denied for another endpoint')
        if self.ref and actual.scheme != 'https' and actual.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise PermissionError('Secret use requires TLS or loopback')
        value = ''
        if self.ref:
            grant = _grant(self.__scope, self.ref, self.__endpoint, 'vendor.authenticate')
            value = str(self.__scope._store._read(grant, target=self.__endpoint, operation='vendor.authenticate'))
        return ({'x-api-key': value, 'anthropic-version': '2023-06-01'} if wire == 'anthropic'
                else {'Authorization': 'Bearer ' + value})


class SecretLoggingFilter(logging.Filter):
    def filter(self, record):
        standard = logging.LogRecord('', 0, '', 0, '', (), None).__dict__
        extra_keys = set(record.__dict__) - set(standard)
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = _exception_text(*record.exc_info)
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
            def log_value(value, depth=0):
                if depth > 64:
                    raise ValueError('Diagnostic nesting limit')
                if isinstance(value, dict):
                    return {str(k): log_value(v, depth + 1) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [log_value(v, depth + 1) for v in value]
                if isinstance(value, (str, int, float, bool)) or value is None:
                    return value
                return str(value)
            extras = {k: record.__dict__[k] for k in extra_keys}
            safe_extras = redact(log_value(extras))
            for key in standard:
                if key != 'msg' and isinstance(record.__dict__.get(key), str):
                    record.__dict__[key] = redact(record.__dict__[key])
            # Replace, rather than overlay: a sanitized key must not leave its
            # original spelling/value available to a structured log handler.
            for key in extra_keys:
                record.__dict__.pop(key, None)
            record.__dict__.update(safe_extras)
        except Exception:
            # Failure is a closed output boundary, including structured extras.
            for key in extra_keys:
                record.__dict__.pop(key, None)
            for key in standard:
                if isinstance(record.__dict__.get(key), str):
                    record.__dict__[key] = MARKER
            record.msg, record.args = MARKER + ' (diagnostic suppressed)', ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def _exception_text(exc_type, value, tb):
    import traceback
    try:
        return redact(''.join(traceback.format_exception(exc_type, value, tb)))
    except Exception:
        return MARKER + ' (diagnostic suppressed)\n'


def install_tk_exception_redaction(root):
    previous = root.report_callback_exception
    if getattr(previous, '_forge_secrets', False): return
    def safe_callback(exc_type, value, tb):
        # Do not hand an unfiltered traceback/exception to a crash reporter.
        previous(RuntimeError, RuntimeError(_exception_text(exc_type, value, tb)), None)
    safe_callback._forge_secrets = True
    root.report_callback_exception = safe_callback


def install_logging_redaction():
    """Cover later handlers, logger extras, and unhandled process/thread errors."""
    with _LOCK:
        import sys
        if not getattr(sys.excepthook, '_forge_secrets', False):
            def safe_error(exc_type, value, tb):
                sys.stderr.write(_exception_text(exc_type, value, tb))
            safe_error._forge_secrets = True
            sys.excepthook = safe_error
        if not getattr(threading.excepthook, '_forge_secrets', False):
            def safe_thread(args):
                if args.exc_type is not SystemExit:
                    sys.stderr.write(_exception_text(args.exc_type, args.exc_value, args.exc_traceback))
            safe_thread._forge_secrets = True
            threading.excepthook = safe_thread
        make_record = logging.Logger.makeRecord
        if not getattr(make_record, '_forge_secrets', False):
            def safe_record(self, *args, **kwargs):
                record = make_record(self, *args, **kwargs)
                SecretLoggingFilter().filter(record)
                return record
            safe_record._forge_secrets = True
            logging.Logger.makeRecord = safe_record
        factory = logging.getLogRecordFactory()
        if getattr(factory, '_forge_secrets', False):
            return
        def safe_factory(*args, **kwargs):
            record = factory(*args, **kwargs)
            SecretLoggingFilter().filter(record)
            return record
        safe_factory._forge_secrets = True
        logging.setLogRecordFactory(safe_factory)


class StreamingRedactor:
    """Hold a possible secret prefix across callbacks before exposing it."""
    def __init__(self):
        self.pending = ''

    def feed(self, text: str, *, final=False) -> str:
        self.pending += text
        with _LOCK:
            values = tuple(_KNOWN)
        for value in sorted(values, key=len, reverse=True):
            self.pending = self.pending.replace(value, MARKER)
        keep = 0
        if not final:
            for value in values:
                for n in range(min(len(value) - 1, len(self.pending)), 0, -1):
                    if self.pending.endswith(value[:n]):
                        keep = max(keep, n)
                        break
        if keep:
            out, self.pending = self.pending[:-keep], self.pending[-keep:]
        else:
            out, self.pending = self.pending, ''
        return out
