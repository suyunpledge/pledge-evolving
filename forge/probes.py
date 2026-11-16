"""Host-owned observations for executable acceptance; no model-callable hooks.

Loom supplies environment probes; LangGraph supplies fresh host-bound reads.
Registries are immutable and each attempt executes a fresh observation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import threading
import time

from .secrets import read_public_bytes


def json_value(value, depth=0):
    """Bounded, finite JSON only; detach caller-owned containers."""
    if depth > 16:
        raise ValueError('Probe JSON nesting exceeds 16')
    kind = type(value)
    if kind is str and len(value) > 32768:
        raise ValueError('Probe JSON string exceeds 32768 characters')
    if kind in (type(None), bool, int, str):
        result = value
    elif kind is float and math.isfinite(value):
        result = value
    elif kind is list and len(value) <= 1024:
        result = [json_value(item, depth+1) for item in value]
    elif kind is dict and len(value) <= 128 and all(type(k) is str for k in value):
        result = {k: json_value(v, depth+1) for k, v in value.items()}
    else:
        raise ValueError('Probe values must be bounded finite JSON')
    if depth == 0 and len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > 32768:
        raise ValueError('Probe JSON exceeds 32768 characters')
    return result


def canonical(value):
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}', value):
        raise ValueError('Invalid environment/probe name')
    return value


def file_input(value, *, pointer=False):
    allowed = {'path', 'pointer'} if pointer else {'path'}
    if set(value) - allowed:
        raise ValueError('Unknown workspace probe input fields')
    path = value.get('path')
    if (not isinstance(path, str) or not path or '\0' in path or len(path) > 1024
            or Path(path).is_absolute() or PureWindowsPath(path).drive
            or '..' in path.replace('\\', '/').split('/')):
        raise ValueError('Probe path must stay relative to workspace')
    if pointer:
        p = value.get('pointer', '')
        if not isinstance(p, str) or (p and not p.startswith('/')) or re.search(r'~(?![01])', p):
            raise ValueError('Invalid JSON pointer')


@dataclass(frozen=True)
class ProbeSpec:
    env: str
    name: str
    observe: object
    validate: object = None


@dataclass(frozen=True)
class ProbeContext:
    workspace: Path
    run_id: str
    deadline: float
    cancellation: threading.Event
    _check_host: object
    _read: object

    def check_cancel(self):
        self._check_host()
        if self.cancellation.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError('Probe observation expired or cancelled')

    def read_text(self, path):
        self.check_cancel()
        value = self._read(path)
        self.check_cancel()
        return value

    def file_digest(self, path):
        self.check_cancel()
        value = self._read(path, digest_only=True)
        self.check_cancel()
        return value


_active_lock = threading.Lock()
_active = set()


@dataclass(frozen=True)
class ProbeRegistry:
    specs: tuple = ()

    def __post_init__(self):
        specs = tuple(self.specs)
        keys = set()
        for spec in specs:
            if not isinstance(spec, ProbeSpec) or not callable(spec.observe):
                raise ValueError('Host must supply ProbeSpec with a callable observer')
            key = (identifier(spec.env), identifier(spec.name))
            if key in keys or spec.validate is not None and not callable(spec.validate):
                raise ValueError('Duplicate or invalid host probe')
            keys.add(key)
        object.__setattr__(self, 'specs', specs)

    @classmethod
    def defaults(cls):
        return cls((ProbeSpec('workspace', 'json_value', _json_probe,
                             lambda v: file_input(v, pointer=True)),
                    ProbeSpec('workspace', 'file_digest', _digest_probe, file_input)))

    def with_probe(self, env, name, observe, *, validate=None):
        return ProbeRegistry((*self.specs, ProbeSpec(env, name, observe, validate)))

    def resolve(self, env, name, value):
        spec = next((s for s in self.specs if (s.env, s.name) == (env, name)), None)
        if spec is None:
            raise ValueError(f'Unknown host environment probe: {env}.{name}')
        if spec.validate:
            spec.validate(json_value(value))
        return spec

    def validate_contract(self, contract):
        for criterion in contract.criteria:
            if criterion.kind == 'env_probe':
                self.resolve(criterion.env, criterion.probe, criterion.input)

    def check(self, criterion, *, workspace, context, registry, check_cancel):
        spec = self.resolve(criterion.env, criterion.probe, criterion.input)
        cancel, done = threading.Event(), threading.Event()
        deadline = time.monotonic() + criterion.timeout
        root = Path(workspace).resolve()
        key = (os.path.normcase(str(root)), criterion.env, criterion.probe)
        evidence = {'env': criterion.env, 'probe': criterion.probe, 'trust': 'untrusted'}
        files = []

        def read(path, digest_only=False):
            if len(files) >= 32:
                raise ValueError('Probe may inspect at most 32 files')
            target = (root / path).resolve()
            if not target.is_relative_to(root):
                raise PermissionError('Probe path/symlink escapes workspace')
            denial = registry.authorize('read_file', {'path': str(target)}, context)
            if denial is not None:
                raise PermissionError(denial.error or 'Probe read denied')
            data = read_public_bytes(target, limit=8*1024*1024)
            digest = hashlib.sha256(data).hexdigest()
            files.append({'path': str(target.relative_to(root)),
                          'sha256': digest})
            return digest if digest_only else context.secret_scope.document_text(target, data.decode('utf-8-sig'))

        probe_context = ProbeContext(root, str(context.extras.get('run_id', '')),
                                     deadline, cancel, check_cancel, read)
        outcome = {}
        with _active_lock:
            if key in _active or len(_active) >= 32:
                return {'passed': False, 'detail': 'Probe is busy or host probe capacity reached',
                        'evidence': {**evidence, 'status': 'busy'}, 'unknown_outcome': True}
            _active.add(key)

        def worker():
            try:
                probe_context.check_cancel()
                actual = json_value(spec.observe(probe_context, json_value(criterion.input)))
                probe_context.check_cancel()
                outcome['actual'] = actual
            except BaseException as exc:
                outcome['error'] = f'{type(exc).__name__}: {exc}'
            finally:
                with _active_lock:
                    _active.discard(key)
                done.set()

        thread = threading.Thread(target=worker, daemon=True, name='forge-probe')
        try:
            thread.start()
            while not done.wait(min(.02, max(0, deadline-time.monotonic()))):
                check_cancel()
                if time.monotonic() >= deadline:
                    cancel.set()
                    return {'passed': False, 'detail': 'Probe timed out; no automatic replay',
                            'evidence': {**evidence, 'status': 'timeout'}, 'unknown_outcome': True}
            check_cancel()
            if time.monotonic() >= deadline:
                return {'passed': False, 'detail': 'Probe exceeded deadline',
                        'evidence': {**evidence, 'status': 'timeout'}, 'unknown_outcome': True}
            if 'error' in outcome:
                return {'passed': False, 'detail': context.secret_scope.protect_text(outcome['error'])[:2000],
                        'evidence': {**evidence, 'status': 'error'}, 'unknown_outcome': False}
            actual = outcome['actual']
            passed = canonical(actual) == canonical(criterion.expect)
            safe = context.secret_scope.protect({'actual': actual, 'expect': criterion.expect})
            return {'passed': passed, 'detail': 'Environment probe passed' if passed else 'Environment probe mismatch',
                    'evidence': {**evidence, 'status': 'completed', 'files': files, **safe},
                    'unknown_outcome': False}
        finally:
            cancel.set()
            if thread.ident is None:
                with _active_lock:
                    _active.discard(key)


def _json_probe(ctx, value):
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = val
        return result
    def nonfinite(value):
        raise ValueError('Non-finite JSON')
    actual = json.loads(ctx.read_text(value['path']), parse_constant=nonfinite, object_pairs_hook=pairs)
    pointer = value.get('pointer', '')
    for token in pointer.split('/')[1:] if pointer else ():
        ctx.check_cancel()
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(actual, list):
            if not re.fullmatch(r'0|[1-9][0-9]*', token):
                raise ValueError('Invalid JSON array index')
            actual = actual[int(token)]
        elif isinstance(actual, dict):
            actual = actual[token]
        else:
            raise ValueError('JSON pointer traverses a scalar')
    return json_value(actual)


def _digest_probe(ctx, value):
    # Digest the exact source bytes recorded by the mediated read, including BOM/newlines.
    return ctx.file_digest(value['path'])
