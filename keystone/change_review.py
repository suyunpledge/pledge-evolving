"""Per-action file receipts and revision-checked, file-scoped undo.

Snapshots live in Keystone home, never in the user's Git index. Only mediated
file writes are captured; shell/connector side effects are not inferred from
repository dirt. Raw bytes stay on the host; public previews are redacted.
"""
from contextlib import ExitStack
import base64
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .runtime_lock import resource_lock
from .secrets import assert_public_path, read_public_bytes, SecretScope, REF, MARKER, redact, protect_store_path

LIMIT = 2 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def atomic_bytes(path, raw, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.keystone-change-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ChangeReviewStore:
    def __init__(self, home, workspace):
        self.workspace = Path(workspace).resolve()
        key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.root = Path(home) / 'changes' / key
        protect_store_path(Path(home) / 'changes')

    def path(self, value):
        target = Path(value)
        if not target.is_absolute():
            target = self.workspace / target
        # Reject links/junctions, including links introduced after recording.
        part = Path(target.anchor)
        for name in target.parts[1:]:
            part /= name
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('变更路径包含链接，无法安全回退')
        resolved = target.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError('变更不在当前工作区内')
        relative = resolved.relative_to(self.workspace)
        if not relative.parts or any(p.casefold() == '.git' for p in relative.parts):
            raise ValueError('不能回退工作区根目录或 Git 元数据')
        assert_public_path(resolved)
        return resolved

    def capture(self, value):
        path = self.path(value)
        raw = read_public_bytes(path, limit=LIMIT) if path.exists() else None
        return {'raw': raw, 'mode': stat.S_IMODE(path.stat().st_mode) if raw is not None else None}

    def record(self, value, before):
        path = self.path(value)
        after = self.capture(path)
        if before['raw'] == after['raw']:
            return None
        identifier = uuid.uuid4().hex
        record = {'version': 1, 'id': identifier, 'path': path.relative_to(self.workspace).as_posix(),
                  'before': self._encode(before['raw']), 'after': self._encode(after['raw']),
                  'mode': before['mode']}
        atomic_bytes(self.root / (identifier + '.json'), json.dumps(record).encode(), 0o600)
        return identifier

    @staticmethod
    def _encode(raw):
        return base64.b64encode(raw).decode() if raw is not None else None

    @staticmethod
    def _decode(value):
        raw = base64.b64decode(value, validate=True) if value is not None else None
        if raw is not None and len(raw) > LIMIT:
            raise ValueError('变更快照过大')
        return raw

    def _load(self, identifiers):
        if (not isinstance(identifiers, list) or not identifiers or len(identifiers) > 256
                or any(not isinstance(i, str) for i in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError('无效的变更记录列表')
        groups = {}
        total = 0
        for identifier in identifiers:
            if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
                raise ValueError('无效的变更记录')
            source = self.root / (identifier + '.json')
            size = source.stat().st_size
            total += size
            if size > LIMIT * 3 or total > 64 * 1024 * 1024:
                raise ValueError('变更记录过大')
            record = json.loads(source.read_text(encoding='utf-8'))
            if record.get('id') != identifier or record.get('version') != 1:
                raise ValueError('变更记录不匹配')
            path = self.path(record['path'])
            before, after = self._decode(record['before']), self._decode(record['after'])
            group = groups.setdefault(str(path), {'path': path, 'before': before, 'after': before,
                'mode': record['mode'], 'ids': [], 'conflict': False, 'restored': True})
            group['conflict'] |= group['after'] != before
            group['after'] = after
            group['ids'].append(identifier)
            group['restored'] &= (self.root / (identifier + '.undone')).exists()
        return [g for g in groups.values() if g['before'] != g['after']]

    def _preview(self, group):
        path = group['path']
        current = self.capture(path)['raw']
        conflict = group['conflict'] or (not group['restored'] and current != group['after'])
        before, after = group['before'], group['after']
        preview, added, removed = None, None, None
        scope = SecretScope()
        try:
            if max(len(before or b''), len(after or b'')) > 256 * 1024:
                raise ValueError('large preview')
            texts = [REF.sub(MARKER, scope.document_text(path, (raw or b'').decode('utf-8')))
                     for raw in (before, after)]
            if any('\0' in text for text in texts):
                raise ValueError('binary')
            lines = list(difflib.unified_diff(texts[0].splitlines(), texts[1].splitlines(),
                fromfile='a/' + path.name if before is not None else '/dev/null',
                tofile='b/' + path.name if after is not None else '/dev/null', lineterm=''))
            # Count from opcodes so content beginning with +++/--- is counted.
            ops = difflib.SequenceMatcher(None, texts[0].splitlines(), texts[1].splitlines()).get_opcodes()
            added = sum(j2-j1 for tag, i1, i2, j1, j2 in ops if tag in ('insert', 'replace'))
            removed = sum(i2-i1 for tag, i1, i2, j1, j2 in ops if tag in ('delete', 'replace'))
            preview = str(redact('\n'.join(lines)))
        except (UnicodeError, ValueError):
            pass
        finally:
            scope.close()
        return {'path': str(path), 'name': path.relative_to(self.workspace).as_posix(),
                'kind': 'created' if before is None else 'deleted' if after is None else 'modified',
                'added': added, 'removed': removed, 'diff': (preview or '')[:65536],
                'truncated': preview is not None and len(preview) > 65536,
                'binary': preview is None, 'conflict': conflict, 'restored': group['restored'],
                'undoTool': 'delete_file' if before is None else 'write_file'}

    def review(self, identifiers, authorize=None):
        with resource_lock('workspace-write', self.workspace):
            groups = self._load(identifiers)
            if authorize:
                for group in groups:
                    authorize('read_file', {'path': str(group['path'])})
            files = [self._preview(group) for group in groups]
            return {'files': files, 'canRevert': any(not f['restored'] for f in files) and all(not f['conflict'] for f in files),
                    'restored': bool(files) and all(f['restored'] for f in files)}

    def revert(self, identifiers, authorize):
        with resource_lock('workspace-write', self.workspace), ExitStack() as locks:
            groups = self._load(identifiers)
            for group in sorted(groups, key=lambda g: str(g['path'])):
                locks.enter_context(resource_lock('file-write', group['path']))
            # Validate the entire batch before changing any file.
            for group in groups:
                if group['restored']:
                    continue
                path = self.path(group['path'])
                if group['conflict'] or self.capture(path)['raw'] != group['after']:
                    raise ValueError('文件已有后续修改，未执行回退：' + path.name)
                authorize('delete_file' if group['before'] is None else 'write_file', {'path': str(path)})
            for group in groups:
                if group['restored']:
                    continue
                path = self.path(group['path'])
                if self.capture(path)['raw'] != group['after']:
                    raise ValueError('回退期间文件发生变化，已停止：' + path.name)
                if group['before'] is None:
                    path.unlink()
                else:
                    atomic_bytes(path, group['before'], group['mode'])
                for identifier in group['ids']:
                    atomic_bytes(self.root / (identifier + '.undone'), b'1', 0o600)
            return self.review(identifiers)
