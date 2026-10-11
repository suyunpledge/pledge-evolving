"""Adapter over Forge's existing workspace data sources (no second crawler)."""
from pathlib import Path
import difflib

from workspace import _scan_tree, _git_status_map, _git_diff, _run_git
from forge.development.editor import ProjectBoundary, Document
from forge.secrets import redact, SecretScope, REF, MARKER


class WorkspaceSource:
    def __init__(self, root):
        self.boundary = ProjectBoundary(root)
        self.root = self.boundary.root

    def status(self):
        git_root = _run_git(self.root, 'rev-parse', '--show-toplevel').strip()
        base = Path(git_root).resolve() if git_root else self.root
        status = {}
        for value, code in _git_status_map(self.root).items():
            try:
                path = self.boundary.path(base / value)
            except (PermissionError, OSError, ValueError):
                continue
            status[path.relative_to(self.root).as_posix()] = code
        return status

    def scan(self, expanded, search=''):
        nodes = _scan_tree(self.root, expanded=None if search else expanded, max_nodes=2000)
        # No symlink targets outside the project, store files or Git metadata in the UI.
        safe = []
        for node in nodes:
            try:
                path = self.boundary.path(node['path'])
            except (PermissionError, OSError, ValueError):
                continue
            if search and (node['is_dir'] or search.casefold() not in path.name.casefold()):
                continue
            safe.append({**node, 'path': str(path)})
        return {'nodes': safe, 'status': self.status(),
                'branch': _run_git(self.root, 'branch', '--show-current').strip() or '(detached / no Git)'}

    def diff(self, path, view='combined'):
        path = self.boundary.path(path)
        if view not in {'combined', 'staged', 'working'}:
            raise ValueError('Unknown Git diff view')
        relative = path.relative_to(self.root).as_posix()
        if self.status().get(relative, '').startswith('?'):
            if view == 'staged':
                return 'Untracked file is not staged.'
            document = Document.open(self.boundary, path)
            scope = SecretScope()
            try:
                safe = REF.sub(MARKER, scope.document_text(path, document.text))
            finally:
                scope.close()
            return redact(''.join(difflib.unified_diff([], safe.splitlines(True),
                          fromfile='/dev/null', tofile=relative))) or 'Untracked empty file.'
        # Pure read-only Git command, using --no-ext-diff/--no-textconv from existing helper.
        if view == 'combined':
            value = _git_diff(self.root, path=path)
        else:
            args = ['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--no-renames']
            if view == 'staged':
                args.append('--cached')
            value = _run_git(self.root, *args, '--', str(path))
        return redact(value) or 'No diff in the selected view.'
