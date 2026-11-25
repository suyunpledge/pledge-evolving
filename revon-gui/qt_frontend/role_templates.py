"""Role templates (角色模板) for the Qt app: pick, switch, duplicate and customise.

The data and the safety rules live in ``revon.agent_templates``; this is only
the QObject the QML binds to. The active choice is a desktop preference
(``gui/qt/role-template.json``); user copies are written to ``<home>/agents``
exactly as the CLI's ``revon templates duplicate`` does, so both front ends
see the same library.
"""
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Property, Signal, Slot

from revon.agent_templates import (TemplateError, delete_template, duplicate_template, load_templates,
                                      save_template)


class RoleTemplates(QObject):
    changed = Signal()

    def __init__(self, owner, home):
        super().__init__(owner)
        self.owner = owner
        # The preview never writes into the real home.
        self._scratch = tempfile.TemporaryDirectory(prefix='revon-roles-preview-',
                                                    ignore_cleanup_errors=True) if owner.demo else None
        self.home = Path(self._scratch.name) if self._scratch else Path(home)
        self.path = owner.store.folder / 'role-template.json'
        stored = {} if owner.demo else owner.store.read(self.path, {})
        self._active_id = stored.get('active', '') if isinstance(stored, dict) else ''
        self._error = ''
        self._templates, self._problems = [], []
        self._reload()
        if owner.demo and not self._active_id:
            self._active_id = 'fullstack'

    # -- data -------------------------------------------------------------
    def _reload(self):
        try:
            self._templates, self._problems = load_templates(self.home)
        except TemplateError as exc:  # a broken built-in is a packaging bug; keep the app usable
            self._templates, self._problems = [], [str(exc)]
        if self._active_id and self.active() is None:
            self._active_id = ''

    def active(self):
        """The active ``AgentTemplate`` or None (general assistant)."""
        return next((t for t in self._templates if t.id == self._active_id), None)

    def _row(self, template):
        row = template.summary()
        row['active'] = template.id == self._active_id
        row['permission'] = {'read-only': '只读', 'plan': '只规划', 'default': '写入前询问',
                             'acceptEdits': '工作区内直写', 'auto': '自动'}.get(row['mode'], row['mode'])
        return row

    rows = Property('QVariantList', lambda self: [self._row(t) for t in self._templates], notify=changed)
    activeId = Property(str, lambda self: self._active_id, notify=changed)
    activeName = Property(str, lambda self: self.active().name if self.active() else '通用助手', notify=changed)
    activeIcon = Property(str, lambda self: self.active().icon if self.active() else 'bot', notify=changed)
    activeDescription = Property(str, lambda self: self.active().description if self.active()
                                 else '不使用角色模板，按你的权限与工具设置工作。', notify=changed)
    activeStarters = Property('QStringList', lambda self: list(self.active().starters) if self.active() else [],
                              notify=changed)
    error = Property(str, lambda self: self._error or ('；'.join(self._problems[:2])), notify=changed)

    # -- actions ----------------------------------------------------------
    def _persist(self):
        if self.owner.demo:
            return
        try:
            self.owner.store.atomic(self.path, {'active': self._active_id})
        except OSError:
            self._error = '无法保存角色选择。'

    def _blocked(self):
        if self.owner.busy:
            self._error = '正在生成，完成后再切换角色。'
            self.changed.emit()
            return True
        return False

    @Slot(str)
    def select(self, template_id):
        if self._blocked() or template_id == self._active_id:
            return
        if template_id and not any(t.id == template_id for t in self._templates):
            return
        self._active_id, self._error = template_id, ''
        self._persist()
        self.changed.emit()
        self.owner._tool_settings._publish()  # the catalog shows which tools this role exposes
        self.owner.changed.emit()

    @Slot(str, result=str)
    def duplicate(self, template_id):
        try:
            copy = duplicate_template(self.home, template_id)
        except (TemplateError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return ''
        self._error = ''
        self._reload()
        self.changed.emit()
        return copy.id

    @Slot(str, str, str, str, str, result=bool)
    def save(self, template_id, name, description, guidance, starters):
        current = next((t for t in self._templates if t.id == template_id), None)
        if current is None or current.builtin:
            self._error = '内置模板不能直接修改，请先复制。'
            self.changed.emit()
            return False
        raw = current.to_raw()
        raw.update(name=name.strip(), description=description.strip(), guidance=guidance.strip(),
                   starters=[line.strip() for line in starters.splitlines() if line.strip()][:8])
        try:
            save_template(self.home, raw)
        except (TemplateError, OSError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._error = ''
        self._reload()
        self.changed.emit()
        self.owner.changed.emit()
        return True

    @Slot(str)
    def remove(self, template_id):
        if self._blocked():
            return
        try:
            delete_template(self.home, template_id)
        except (TemplateError, OSError) as exc:
            self._error = str(exc)
        else:
            self._error = ''
            if template_id == self._active_id:
                self._active_id = ''
                self._persist()
        self._reload()
        self.changed.emit()
        self.owner.changed.emit()

    @Slot()
    def refresh(self):
        self._error = ''
        self._reload()
        self.changed.emit()

    @Slot(str)
    def useStarter(self, text):
        """Put a starter prompt in the composer (the user still presses send)."""
        self.owner.draft = text
        self.owner.focusComposer.emit()
