"""Development-only revision guard; every invocation still passes ToolRegistry."""
from contextlib import contextmanager
from threading import RLock
from weakref import WeakValueDictionary

from .editor import ProjectBoundary, fingerprint
from forge.secrets import read_public_bytes
from forge.secrets import SecretScope, redact, REF, MARKER
import difflib

_locks = WeakValueDictionary()
_locks_lock = RLock()
WRITES = {'write_file', 'edit_file', 'apply_patch', 'delete_file', 'edit_config'}
READS = {'read_file', 'read_range', 'file_outline', 'edit_config'}


class FileRevisionGuard:
    def __init__(self, workspace):
        self.boundary = ProjectBoundary(workspace)
        self.revisions = {}
        key = str(self.boundary.root).casefold()
        with _locks_lock:
            self.lock = _locks.setdefault(key, RLock())

    def fork(self):
        return FileRevisionGuard(self.boundary.root)

    def digest(self, path):
        return fingerprint(read_public_bytes(path, limit=8 * 1024 * 1024)) if path.exists() else None

    def targets(self, args):
        paths = [args.get('path')]
        blocks = args.get('patches') or args.get('edits') or []
        if isinstance(blocks, list):
            paths.extend(b.get('path') for b in blocks if isinstance(b, dict))
        return {str(self.boundary.path(p)) for p in paths if isinstance(p, str) and p}

    @contextmanager
    def protect(self, name, args, ctx):
        write = name in WRITES and (name != 'edit_config' or args.get('patch') is not None)
        if not write and name not in READS:
            # Never hold the lock across spawn/model requests.
            yield args
            return
        with self.lock:
            paths = self.targets(args)
            guarded = dict(args)
            self._before = {target: self.digest(self.boundary.path(target)) for target in paths}
            self._before_text = {}
            if write:
                for target in paths:
                    self._before_text[target] = self._public_text(target)
            if write:
                revisions = {}
                for target in paths:
                    current = self._before[target]
                    if target not in self.revisions and current is not None:
                        raise ValueError('Read the existing file before editing it: ' + target)
                    revisions[target] = self.revisions.get(target)
                supplied = args.get('_expected_revisions')
                if supplied is not None and supplied != revisions:
                    raise ValueError('Explicit preview does not match this Agent observation')
                guarded['_expected_revisions'] = revisions
            yield guarded

    def record(self, name, args, result):
        if result.ok and name in READS | WRITES:
            observed = {}
            for target in self.targets(args):
                observed[target] = self.digest(self.boundary.path(target))
            read = name in READS and (name != 'edit_config' or args.get('patch') is None)
            if read and observed != self._before:
                result.ok, result.error = False, 'File changed during read; read it again before editing'
                result.content = ''
                result.meta['boundary'] = 'file.revision'
                return
            self.revisions.update(observed)
            if name in WRITES and not read:
                changes = []
                preview_budget = 64 * 1024
                for target, revision in observed.items():
                    before = self._before_text.get(target)
                    after = self._public_text(target)
                    if revision == self._before.get(target):
                        continue
                    diff = None if before is None or after is None else ''.join(difflib.unified_diff(
                        before.splitlines(True), after.splitlines(True), fromfile='before/' + target,
                        tofile='after/' + target))
                    preview = redact(diff) if diff is not None else None
                    truncated = preview is not None and len(preview) > min(16384, preview_budget)
                    if preview is not None:
                        preview = preview[:min(16384, preview_budget)]
                        preview_budget -= len(preview)
                    changes.append({'path': target, 'before_revision': self._before.get(target),
                        'revision': revision, 'state': 'written', 'diff': preview,
                        'diff_truncated': truncated, 'preview_only': True,
                        'added': sum(line.startswith('+') and not line.startswith('+++') for line in (diff or '').splitlines()) if diff is not None else None,
                        'removed': sum(line.startswith('-') and not line.startswith('---') for line in (diff or '').splitlines()) if diff is not None else None})
                result.meta['file_changes'] = changes

    def _public_text(self, target):
        """Best-effort presentation only; unavailable previews never block a write."""
        path = self.boundary.path(target)
        if not path.exists():
            return ''
        scope = SecretScope()
        try:
            raw = read_public_bytes(path, limit=512 * 1024)
            if b'\0' in raw:
                return None
            return REF.sub(MARKER, scope.document_text(path, raw.decode('utf-8').replace('\r\n', '\n')))
        except (ValueError, PermissionError, OSError, UnicodeError):
            return None
        finally:
            scope.close()
