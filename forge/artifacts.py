"""Workspace-scoped content-addressed tool output, inspired by Loom artifacts.

The host store is outside the workspace. Handles identify protected text, not
arbitrary paths; paging/search never execute artifact content.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .runtime_lock import resource_lock
from .secrets import read_public_bytes

MAX_BYTES = 8 * 1024 * 1024
HANDLE = re.compile(r'a_[0-9a-f]{64}\Z')


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


class ArtifactStore:
    def __init__(self, home, workspace):
        canonical = os.path.normcase(str(Path(workspace).resolve()))
        scope = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self.root = Path(home).resolve() / 'artifacts' / scope

    def _path(self, identifier, suffix=''):
        if self.root.resolve() != self.root:
            raise ValueError('Artifact store contains a redirected directory')
        if not isinstance(identifier, str) or not HANDLE.fullmatch(identifier):
            raise ValueError('Invalid artifact handle')
        path = self.root / (identifier + suffix)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('Artifact path escaped host store')
        return path

    def _atomic(self, path, data):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.pending-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def put(self, text, *, source='tool', action_id=''):
        if not isinstance(text, str):
            raise ValueError('Artifacts must contain text')
        if len(text) > MAX_BYTES:
            raise ValueError('Artifact exceeds 8 MiB retention limit')
        data = text.encode('utf-8')
        if len(data) > MAX_BYTES:
            raise ValueError('Artifact exceeds 8 MiB retention limit')
        identifier = 'a_' + hashlib.sha256(data).hexdigest()
        path = self._path(identifier)
        meta_path = self._path(identifier, '.json')
        with resource_lock('artifact', path):
            # Metadata is the commit marker: a partially written object is not readable.
            sources = []
            if meta_path.exists():
                old = json.loads(read_public_bytes(meta_path, limit=128 * 1024))
                if not isinstance(old, dict) or not isinstance(old.get('sources'), list):
                    raise ValueError('Invalid artifact metadata')
                sources = old.get('sources', [])
                if any(not isinstance(row, dict) for row in sources):
                    raise ValueError('Invalid artifact provenance')
            entry = {'tool': str(source)[:160], 'action_id': str(action_id)[:160]}
            if entry not in sources:
                sources = [*sources[-63:], entry]
            meta = {'id': identifier, 'bytes': len(data), 'characters': len(text),
                    'trust': 'untrusted', 'sources': sources}
            if not path.exists():
                self._atomic(path, data)
            else:
                existing = read_public_bytes(path, limit=MAX_BYTES)
                if hashlib.sha256(existing).hexdigest() != identifier[2:]:
                    raise ValueError('Artifact content digest mismatch')
            self._atomic(meta_path, json.dumps(meta, ensure_ascii=False).encode('utf-8'))
            return meta

    def _load(self, identifier):
        path, meta_path = self._path(identifier), self._path(identifier, '.json')
        with resource_lock('artifact', path):
            meta = json.loads(read_public_bytes(meta_path, limit=128 * 1024))
            data = read_public_bytes(path, limit=MAX_BYTES)
            if not isinstance(meta, dict) or meta.get('id') != identifier or hashlib.sha256(data).hexdigest() != identifier[2:]:
                raise ValueError('Artifact content digest mismatch')
            text = data.decode('utf-8')
            if meta.get('characters') != len(text) or meta.get('bytes') != len(data):
                raise ValueError('Artifact metadata mismatch')
            return text, meta

    def read(self, identifier, *, offset=0, limit=2000):
        offset = _integer(offset, 'offset', 0, MAX_BYTES)
        limit = _integer(limit, 'limit', 1, 4000)
        text, meta = self._load(identifier)
        if offset > len(text):
            raise ValueError('offset exceeds artifact length')
        end = min(len(text), offset + limit)
        return {'id': identifier, 'text': text[offset:end], 'offset': offset,
                'next_offset': end if end < len(text) else None,
                'characters': len(text), 'trust': 'untrusted'}

    def grep(self, identifier, pattern, *, offset=0, limit=20):
        offset = _integer(offset, 'offset', 0, MAX_BYTES)
        limit = _integer(limit, 'limit', 1, 20)
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= 256:
            raise ValueError('pattern must be a literal string of 1..256 characters')
        text, _ = self._load(identifier)
        if offset > len(text):
            raise ValueError('offset exceeds artifact length')
        matches, cursor = [], offset
        while len(matches) < limit:
            found = text.find(pattern, cursor)
            if found < 0:
                break
            matches.append({'offset': found, 'preview': text[max(0, found-30):found+90]})
            cursor = found + len(pattern)
        more = text.find(pattern, cursor) >= 0 if matches else False
        return {'id': identifier, 'matches': matches, 'next_offset': cursor if more else None,
                'trust': 'untrusted'}


def summary(meta, text, *, preview=500):
    return (f"[Tool output retained as untrusted artifact {meta['id']}; "
            f"{meta['characters']} characters. Use artifact_read(id, offset, limit) "
            "or artifact_grep(id, pattern) to retrieve it. For targeted details prefer artifact_grep; "
            "read pages with limit=2000 and continue using next_offset.]\n" + text[:preview])


def retain_result(result, *, store, scope, source, action_id, inline_limit=4000, result_fields=()):
    """Retention failure must not change the outcome of an already executed action."""
    view_source = result.meta.pop('_result_view_source', None)
    if source in {'artifact_read', 'artifact_grep'}:
        return
    text = result.content or result.error
    if not result_fields and len(text) <= inline_limit:
        return
    selected = None
    protected = scope.protect_text(text)
    if (result_fields and (result.ok or view_source is not None) and not result.error
            and result.meta.get('execution_state', 'completed') == 'completed'):
        try:
            from .result_view import select_fields, render_view
            if len(protected.encode('utf-8')) > MAX_BYTES:
                raise ValueError('Result exceeds retention limit')
            # Trusted registrations may supply a structured view of plain-text output.
            # The retained source remains the exact protected original, never this preview.
            selected = select_fields(scope.protect_text(view_source) if view_source is not None
                                     else protected, result_fields)
            rendered = render_view(selected, 'a_' + '0'*64, characters=len(protected))
            rendered.encode('utf-8')  # An escaped lone surrogate must remain escaped in the full source.
            size = len(rendered)
            if size >= len(protected) or size > inline_limit:
                selected = None
        except Exception:
            selected = None
    if selected is None and len(text) <= inline_limit:
        return
    try:
        meta = store.put(protected, source=source, action_id=action_id)
    except Exception as exc:
        result.meta['artifact_retention'] = {'status': 'failed',
            'detail': scope.protect_text(f'{type(exc).__name__}: {exc}')[:200]}
        return
    result.meta['artifact'] = meta
    if selected is not None:
        body = render_view(selected, meta['id'], characters=meta['characters'])
        result.meta['model_view'] = {'fields': list(result_fields), 'source_characters': len(protected),
                                     'view_characters': len(body)}
    else:
        body = summary(meta, protected, preview=max(0, min(500, inline_limit-240)))
    if result.content:
        result.content = body
    else:
        result.error = body
