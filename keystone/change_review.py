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
import shutil
import stat
import uuid

from .runtime_lock import resource_lock
from .secrets import assert_public_path, read_public_bytes, SecretScope, REF, MARKER, redact, protect_store_path

LIMIT = 2 * 1024 * 1024
DELETE_LIMIT = 8 * 1024 * 1024  # Same ceiling as the file authorization/revision check.


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def atomic_bytes(path, raw, mode=None, *, create_only=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    # On Windows mkstemp may retry an ACL denial as a name collision indefinitely.
    name = path.parent / ('.keystone-change-' + uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    fd = os.open(name, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(name, mode)
        if not create_only:
            os.replace(name, path)
        elif os.name == 'nt':
            os.rename(name, path)  # On Windows rename fails if the destination exists.
        else:
            os.link(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ChangeReviewStore:
    def __init__(self, home, workspace):
        self.workspace = Path(workspace).resolve()
        key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.root = Path(home).resolve() / 'changes' / key
        protect_store_path(self.root.parent)

    def _checked_path(self, value):
        target = Path(value)
        if not target.is_absolute():
            target = self.workspace / target
        # Reject links/junctions, including links introduced after recording.
        part = Path(target.anchor)
        for name in target.parts[1:]:
            part /= name
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('变更路径包含链接，无法安全回退')
        resolved = target.resolve()
        if any(p.casefold() == '.git' for p in resolved.parts):
            raise ValueError('不能操作 Git 元数据')
        assert_public_path(resolved)
        return resolved

    def path(self, value):
        resolved = self._checked_path(value)
        if not resolved.is_relative_to(self.workspace):
            raise ValueError('变更不在当前工作区内')
        if resolved == self.workspace:
            raise ValueError('不能回退工作区根目录')
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
        return self._save(path, before, after['raw'])

    def _save(self, path, before, after):
        identifier = uuid.uuid4().hex
        record = {'version': 1, 'id': identifier, 'path': path.relative_to(self.workspace).as_posix(),
                  'before': self._encode(before['raw']), 'after': self._encode(after),
                  'mode': before['mode']}
        atomic_bytes(self.root / (identifier + '.json'), json.dumps(record).encode(), 0o600)
        return identifier

    def _manual_backup(self, path, raw, mode):
        backups = self.root / 'backups'
        backups.mkdir(parents=True, exist_ok=True)
        folder = backups / uuid.uuid4().hex
        relative = Path('file') / path.name
        manifest = {'original_path': str(path), 'mode': mode, 'sha256': digest(raw)}
        stage = backups / ('.pending-' + folder.name)
        stage.mkdir(mode=0o700)
        try:
            atomic_bytes(stage / relative, raw, 0o600)
            atomic_bytes(stage / 'restore.json', json.dumps(manifest, ensure_ascii=False).encode(), 0o600)
            stage.rename(folder)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return folder / relative

    def delete(self, value, expected_revisions=None, on_prepared=None):
        """Called under the tool's write lock, after policy authorization."""
        path = self._checked_path(value)
        initial = path.stat()
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError('删除工具仅支持普通文件')
        raw = read_public_bytes(path, limit=DELETE_LIMIT)
        if expected_revisions is not None and digest(raw) != expected_revisions[str(path)]:
            raise ValueError('文件已变更，请重新读取后再删除')
        mode = stat.S_IMODE(initial.st_mode)
        recovery = {}
        if path.is_relative_to(self.workspace):
            try:
                identifier = self._save(path, {'raw': raw, 'mode': mode}, None)
                recovery = {'change_ids': [identifier]}
            except OSError:
                pass  # Try a standalone, human-readable backup before allowing deletion.
        if not recovery:
            backup = self._manual_backup(path, raw, mode)
            recovery = {'backup_path': str(backup), 'change_review_incomplete': True}

        if on_prepared is not None:
            on_prepared(path, recovery)
        # An editor outside our locks can replace the source while the recovery copy is flushed.
        current = self._checked_path(value).stat()
        if (not os.path.samestat(initial, current) or initial.st_size != current.st_size
                or initial.st_mtime_ns != current.st_mtime_ns or initial.st_ctime_ns != current.st_ctime_ns
                or read_public_bytes(path, limit=DELETE_LIMIT) != raw):
            raise ValueError('保存恢复记录期间文件发生变化，已取消删除')
        path.unlink()
        return recovery

    @staticmethod
    def _encode(raw):
        return base64.b64encode(raw).decode() if raw is not None else None

    @staticmethod
    def _decode(value, limit=LIMIT):
        raw = base64.b64decode(value, validate=True) if value is not None else None
        if raw is not None and len(raw) > limit:
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
            if size > DELETE_LIMIT * 2 or total > 64 * 1024 * 1024:
                raise ValueError('变更记录过大')
            record = json.loads(source.read_text(encoding='utf-8'))
            if record.get('id') != identifier or record.get('version') != 1:
                raise ValueError('变更记录不匹配')
            path = self.path(record['path'])
            after = self._decode(record['after'])
            before = self._decode(record['before'], DELETE_LIMIT if after is None else LIMIT)
            group = groups.setdefault(str(path), {'path': path, 'before': before, 'after': before,
                'mode': record['mode'], 'ids': [], 'conflict': False, 'restored': True})
            group['conflict'] |= group['after'] != before
            group['after'] = after
            group['ids'].append(identifier)
            group['restored'] &= (self.root / (identifier + '.undone')).exists()
        return [g for g in groups.values() if g['before'] != g['after']]

    def _matches_after(self, path, after):
        if after is None:
            return not path.exists()
        return self.capture(path)['raw'] == after

    def _preview(self, group):
        path = group['path']
        conflict = group['conflict'] or (not group['restored'] and not self._matches_after(path, group['after']))
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
                if group['conflict'] or not self._matches_after(path, group['after']):
                    raise ValueError('文件已有后续修改，未执行回退：' + path.name)
                authorize('delete_file' if group['before'] is None else 'write_file', {'path': str(path)})
            for group in groups:
                if group['restored']:
                    continue
                path = self.path(group['path'])
                if not self._matches_after(path, group['after']):
                    raise ValueError('回退期间文件发生变化，已停止：' + path.name)
                if group['before'] is None:
                    path.unlink()
                else:
                    atomic_bytes(path, group['before'], group['mode'], create_only=group['after'] is None)
                for identifier in group['ids']:
                    atomic_bytes(self.root / (identifier + '.undone'), b'1', 0o600)
            return self.review(identifiers)
