"""Shared visual components; no Agent, project, permission or plugin runtime.

Single source of truth: this copy on pledge-evolving mainline is authoritative
(the forge-visual-system-2 worktree holds the already-merged feature branch;
its copy is branch history, not a second runtime). Changes go here first.
"""
from .contracts import WorkspaceKind, DENSITIES, LAYOUTS
from .components import ModeSwitcher, WorkspaceDeck, NoticeBanner, PermissionPrompt

__all__ = ['WorkspaceKind', 'DENSITIES', 'LAYOUTS', 'ModeSwitcher', 'WorkspaceDeck', 'NoticeBanner', 'PermissionPrompt']
