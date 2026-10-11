"""Trusted human editor buffers; never exposed as generic Agent write/resolve tools."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import difflib

from forge.secrets import assert_public_path

MAX_DOCUMENT = 2 * 1024 * 1024


class ConflictError(RuntimeError):
    pass


class ProjectBoundary:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError('Project must be a directory')

    def path(self, value):
        p = Path(value)
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not p.is_relative_to(self.root) or '.git' in p.relative_to(self.root).parts:
            raise PermissionError('File is outside the project or belongs to Git metadata')
        assert_public_path(p)
        return p


def read_bytes(p):
    with p.open('rb') as stream:
        raw = stream.read(MAX_DOCUMENT + 1)
    if len(raw) > MAX_DOCUMENT:
        raise ValueError('Editor file limit is 2 MiB')
    return raw


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass
class Document:
    boundary: ProjectBoundary
    path: Path
    text: str
    encoding: str
    newline: str
    original: str
    digest: str
    status: str = 'clean'
    external: object = None

    @classmethod
    def open(cls, boundary, path, *, encoding=None):
        path = boundary.path(path)
        raw = read_bytes(path)
        encoding = encoding or ('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff'))
                                else 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8')
        if b'\0' in raw and encoding != 'utf-16':
            raise ValueError('Binary files cannot be edited')
        try:
            text = raw.decode(encoding, 'strict')
        except UnicodeError:
            raise ValueError('Encoding is not UTF-8/UTF-16; open with an explicit encoding') from None
        newline = '\r\n' if '\r\n' in text else '\r' if '\r' in text else '\n'
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        return cls(boundary, path, text, encoding, newline, text, fingerprint(raw))

    @property
    def dirty(self):
        return self.text != self.original

    def update(self, text):
        self.text = text
        if self.status != 'conflict':
            self.status = 'modified' if self.dirty else 'clean'

    def check_external(self):
        try:
            changed = fingerprint(read_bytes(self.boundary.path(self.path))) != self.digest
        except (OSError, ValueError, PermissionError):
            changed = True
        if changed:
            self.status = 'conflict'
        return changed

    def inspect_external(self):
        """Worker-side immutable observation. Does not mutate a live editor buffer."""
        try:
            disk = self.open(self.boundary, self.path, encoding=self.encoding)
            return {'document': disk, 'error': None}
        except (OSError, ValueError, UnicodeError, PermissionError) as exc:
            return {'document': None, 'error': str(exc)}

    def reconcile(self, observation):
        """Called on the UI thread, after reading the *latest* user buffer."""
        disk = observation['document']
        if disk is not None and disk.digest == self.digest:
            self.external = None
            if self.status == 'conflict':
                self.status = 'modified' if self.dirty else 'clean'
            return 'unchanged'
        self.external = observation
        if disk is None or self.dirty:
            self.status = 'conflict'
            return 'conflict'
        self.text, self.original, self.digest = disk.text, disk.text, disk.digest
        self.encoding, self.newline, self.status = disk.encoding, disk.newline, 'clean'
        self.external = None
        return 'reloaded'

    def external_diff(self, *, buffer=False):
        if not self.external:
            return 'No external change recorded.'
        disk = self.external['document']
        if disk is None:
            return 'Disk version unavailable: ' + self.external['error']
        left = self.text if buffer else self.original
        return ''.join(difflib.unified_diff(left.splitlines(True), disk.text.splitlines(True),
                       fromfile='editor-buffer' if buffer else 'opened-version', tofile='disk-version'))

    def merge_external(self):
        """Conservative three-way line merge into memory only, never an approval or write."""
        disk = self.external['document'] if self.external else None
        if disk is None:
            raise ConflictError('No readable disk version to merge')
        base = self.original.splitlines(True)
        ours, theirs = self.text.splitlines(True), disk.text.splitlines(True)
        def edits(lines):
            return [(a, b, lines[c:d]) for tag, a, b, c, d in difflib.SequenceMatcher(
                    None, base, lines, autojunk=False).get_opcodes() if tag != 'equal']
        own, other = edits(ours), edits(theirs)
        for a, b, replacement in own:
            for c, d, external in other:
                if (a, b, replacement) == (c, d, external):
                    continue
                # Insertions at changed boundaries are deliberately considered conflicts.
                if max(a, c) < min(b, d) or a == b and c <= a <= d or c == d and a <= c <= b:
                    raise ConflictError('Overlapping edits; compare versions and resolve manually')
        merged = list(base)
        combined = own + [e for e in other if e not in own]
        for a, b, replacement in sorted(combined, key=lambda e: (e[0], e[1]), reverse=True):
            merged[a:b] = replacement
        self.text = ''.join(merged)
        self.original, self.digest = disk.text, disk.digest
        self.encoding, self.newline = disk.encoding, disk.newline
        self.external = None
        self.status = 'modified' if self.dirty else 'clean'

    def save(self):
        try:
            self._save()
        except ConflictError:
            self.status = 'conflict'
            raise
        except Exception:
            self.status = 'failed'
            raise

    def _save(self):
        p = self.boundary.path(self.path)
        if self.check_external():
            raise ConflictError('File changed or was deleted externally; reload/compare before saving')
        raw = self.text.replace('\n', self.newline).encode(self.encoding, 'strict')
        if len(raw) > MAX_DOCUMENT:
            raise ValueError('Editor file limit is 2 MiB')
        fd, name = tempfile.mkstemp(prefix='.' + p.name + '-', suffix='.tmp', dir=p.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(name, stat.S_IMODE(p.stat().st_mode))
            # Recheck after staging, so ordinary concurrent Agent changes are never overwritten.
            if self.boundary.path(self.path) != p or self.check_external():
                raise ConflictError('File changed during save')
            os.replace(name, p)
            self.original, self.digest, self.status = self.text, fingerprint(raw), 'saved'
        except ConflictError:
            self.status = 'conflict'
            raise
        except Exception:
            self.status = 'failed'
            raise
        finally:
            Path(name).unlink(missing_ok=True)

    def reload(self):
        other = self.open(self.boundary, self.path, encoding=self.encoding)
        self.__dict__.update(other.__dict__)
