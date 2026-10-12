"""Visual contracts. Display identities do not change runtime permission modes."""
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from gui_theme import CONTROL_HEIGHT, ICON_HIT_SIZE


class WorkspaceKind(str, Enum):
    WORK = 'work'
    CODING = 'coding'


LABELS = MappingProxyType({WorkspaceKind.WORK: 'Work Mode', WorkspaceKind.CODING: 'Coding Mode'})


@dataclass(frozen=True)
class Density:
    gutter: int
    section_gap: int
    row_height: int
    control_height: int = CONTROL_HEIGHT
    icon_hit_size: int = ICON_HIT_SIZE


# 96-DPI layout units. Typography is shared; density changes spacing.
DENSITIES = MappingProxyType({WorkspaceKind.WORK: Density(20, 16, 36),
                             WorkspaceKind.CODING: Density(8, 8, 28)})


@dataclass(frozen=True)
class LayoutSpec:
    primary: str
    navigation: tuple
    optional_panels: tuple
    presets: tuple


LAYOUTS = MappingProxyType({
    WorkspaceKind.WORK: LayoutSpec('conversation-and-tasks',
        ('conversations', 'tasks', 'agents', 'automations'), ('task-detail', 'context'),
        ('conversation', 'task-focus')),
    WorkspaceKind.CODING: LayoutSpec('editor',
        ('files', 'search', 'git', 'debug'), ('ai', 'diff', 'terminal', 'tasks', 'problems'),
        ('balanced', 'editor-focus', 'review')),
})

# UI placement only. It grants no capability, tool exposure or plugin trust.
EXTENSION_SLOTS = MappingProxyType({
    'work.detail': WorkspaceKind.WORK,
    'work.task-actions': WorkspaceKind.WORK,
    'coding.sidebar': WorkspaceKind.CODING,
    'coding.bottom': WorkspaceKind.CODING,
    'coding.editor-action': WorkspaceKind.CODING,
    'global.notification': None,
    'global.permission': None,
})


@dataclass(frozen=True)
class NoticeSpec:
    tone: str
    title: str
    detail: str
    workspace: WorkspaceKind
    task_id: str = ''

    def __post_init__(self):
        if self.tone not in {'info', 'ok', 'warn', 'error'}:
            raise ValueError('Unknown notification tone')
        if not isinstance(self.workspace, WorkspaceKind):
            raise ValueError('Notification requires its originating workspace')


@dataclass(frozen=True)
class PermissionSpec:
    request_id: str
    workspace: WorkspaceKind
    task_id: str
    project: str
    operation: str
    target: str

    def __post_init__(self):
        if (not isinstance(self.request_id, str) or not self.request_id.strip()
                or not isinstance(self.workspace, WorkspaceKind)):
            raise ValueError('Permission prompt requires an authoritative request reference and origin')
