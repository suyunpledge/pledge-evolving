"""Adapter over Forge's existing workspace data sources (no second crawler)."""
from pathlib import Path

from workspace import _scan_tree, _git_status_map, _git_diff, _run_git
from forge.development.editor import ProjectBoundary
from forge.secrets import redact


class WorkspaceSource:
    def __init__(self, root):
        self.boundary = ProjectBoundary(root)
        self.root = self.boundary.root

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
        return {'nodes': safe, 'status': _git_status_map(self.root),
                'branch': _run_git(self.root, 'branch', '--show-current').strip() or '(detached / no Git)'}

    def diff(self, path):
        path = self.boundary.path(path)
        # Pure read-only Git command, using --no-ext-diff/--no-textconv from existing helper.
        return redact(_git_diff(self.root, path=path)) or 'No tracked diff. Untracked files have no Git base.'
