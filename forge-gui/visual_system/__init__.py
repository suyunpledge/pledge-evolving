"""Shared visual components; no Agent, project, permission or plugin runtime."""
from .contracts import WorkspaceKind, DENSITIES, LAYOUTS
from .components import ModeSwitcher, WorkspaceDeck, NoticeBanner, PermissionPrompt

__all__ = ['WorkspaceKind', 'DENSITIES', 'LAYOUTS', 'ModeSwitcher', 'WorkspaceDeck', 'NoticeBanner', 'PermissionPrompt']
