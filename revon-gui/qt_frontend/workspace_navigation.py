"""Local, read-only navigation for the Qt code workbench."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

from revon.development.editor import Document, MAX_DOCUMENT
from revon.secrets import redact


EXCLUDED = {'.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.revon-build'}
QUICK_LIMIT = 80
MATCH_LIMIT = 400


def linked_directory(path):
    try:
        info = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class Explorer:
    def __init__(self, boundary):
        self.boundary = boundary
        self.expanded = set()
        self.children = {}

    def reveal(self, path):
        for parent in path.relative_to(self.boundary.root).parents:
            if parent.parts:
                self.expanded.add(parent.as_posix())

    def toggle(self, key):
        if key in self.expanded:
            self.expanded.remove(key)
        elif self.boundary.path(key).is_dir():
            self.expanded.add(key)

    def rows(self):
        rows = []

        def visit(directory, depth):
            if directory not in self.children:
                entries = []
                try:
                    with os.scandir(self.boundary.path(directory)) as items:
                        for item in items:
                            if item.name in EXCLUDED or item.is_symlink() or item.is_dir() and linked_directory(item.path):
                                continue
                            try:
                                target = self.boundary.path(item.path)
                                key = target.relative_to(self.boundary.root).as_posix()
                                entries.append((item.is_dir(), item.name, key, str(target)))
                            except (OSError, ValueError):
                                continue
                except OSError:
                    pass
                self.children[directory] = sorted(entries, key=lambda e: (not e[0], e[1].casefold()))
            for folder, name, key, path in self.children[directory]:
                expanded = folder and key in self.expanded
                rows.append(dict(id=key, title=name, path=path, directory=folder, depth=depth, expanded=expanded))
                if expanded:
                    visit(key, depth + 1)
        visit('.', 0)
        return rows


def project_names(boundary, cancel):
    try:
        result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', '.'],
                                cwd=boundary.root, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        for raw in result.stdout.split(b'\0'):
            if cancel.is_set():
                return
            name = os.fsdecode(raw)
            if name and EXCLUDED.isdisjoint(name.split('/')):
                yield name
        return
    for base, dirs, names in os.walk(boundary.root, followlinks=False):
        if cancel.is_set():
            return
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not linked_directory(os.path.join(base, d)))
        for name in sorted(names):
            yield (Path(base) / name).relative_to(boundary.root).as_posix()


def file_rank(path, query):
    name = path.rsplit('/', 1)[-1]
    if not query or name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    if query in path:
        return 3
    for rank, value in ((4, name), (5, path)):
        chars = iter(value)
        if all(c in chars for c in query):
            return rank
    return None


def quick_files(boundary, names, query, recent, cancel):
    query = query.strip().casefold().replace('\\', '/')
    ranked = []
    for name in names:
        if cancel.is_set():
            return []
        path = name.casefold()
        rank = file_rank(path, query)
        if rank is not None:
            ranked.append((rank, recent.get(name, len(recent)), len(path), path, name))
    rows = []
    for *_, name in sorted(ranked):
        if cancel.is_set():
            break
        try:
            target = boundary.path(name)
            if not target.is_file():
                continue
        except (OSError, ValueError):
            continue
        rows.append(dict(path=str(target), name=target.name, relative=name))
        if len(rows) == QUICK_LIMIT:
            break
    return rows


def text_matches(boundary, names, query, buffers, cancel, progress):
    matches = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    last_update = time.monotonic()
    published = 0
    for name in names:
        if cancel.is_set():
            break
        try:
            file = boundary.root / name
            text = buffers.get(name)
            if text is None:
                if not file.is_file() or file.stat().st_size > MAX_DOCUMENT:
                    continue
                doc = Document.open(boundary, file)
                text, file = doc.text, doc.path
            for number, line in enumerate(text.split('\n'), 1):
                for match in pattern.finditer(line):
                    if cancel.is_set():
                        return matches, False
                    if len(matches) == MATCH_LIMIT:
                        return matches, True
                    start, end = match.span()
                    # QTextDocument and QML index UTF-16 code units.
                    column = len(line[:start].encode('utf-16-le')) // 2 + 1
                    length = len(line[start:end].encode('utf-16-le')) // 2
                    before = line[max(0, start - 36):start].lstrip()
                    token = line[start:min(end, start + 96)]
                    after = line[end:end + 80].rstrip()
                    matches.append(dict(path=str(file), relative=name, name=file.name, line=number,
                                        column=column, length=length, before=before, token=token, after=after))
            if len(matches) != published and time.monotonic() - last_update >= .4:
                published = len(matches)
                progress(list(matches))
                last_update = time.monotonic()
        except (OSError, ValueError):
            continue
    return matches, False


class WorkspaceQueries:
    def __init__(self, emit):
        self.emit = emit
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='revon-file-search')
        self.pending = {}

    def reset(self):
        for cancel in self.pending.values():
            cancel.set()
        self.pending.clear()

    def close(self):
        self.reset()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def request(self, pages, row):
        kind, ticket, query = row['kind'], row['ticket'], row['query']
        if kind not in {'quick', 'text'}:
            return
        if kind in self.pending:
            self.pending[kind].set()
        cancel = self.pending[kind] = threading.Event()
        if row.get('cancel') or kind == 'text' and not query:
            return
        boundary = pages.boundary
        docs = list(pages.documents.items())
        buffers = {Path(p).relative_to(boundary.root).as_posix(): d.text for p, d in docs}
        recent_names = sorted(buffers, key=lambda p: str(boundary.root / p) != pages.active_document)
        recent = {p: i for i, p in enumerate(recent_names)}

        def send(rows, *, done=True, limited=False, error=''):
            if not cancel.is_set():
                self.emit('workspace_query', kind=kind, ticket=ticket, root=str(boundary.root),
                          rows=rows, done=done, limited=limited, error=error)

        def run():
            try:
                if cancel.is_set():
                    return
                names = dict.fromkeys(recent)
                for name in project_names(boundary, cancel):
                    names[name] = None
                if kind == 'quick':
                    send(quick_files(boundary, names, query, recent, cancel))
                else:
                    rows, limited = text_matches(boundary, names, query, buffers, cancel,
                                                 lambda partial: send(partial, done=False))
                    send(rows, limited=limited)
            except Exception as exc:
                send([], error=str(redact(str(exc))))
        self.pool.submit(run)
