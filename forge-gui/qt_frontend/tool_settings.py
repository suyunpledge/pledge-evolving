"""Work tool catalog and persistent per-tool choices."""
import threading

from PySide6.QtCore import QObject, Property, Signal, Slot

from .conversation import tool_label, tool_schemas


_GROUPS = {
    '计算': ('calculator', 'unit_convert', 'datetime'),
    '文本': ('text_transform',),
    '搜索': ('public_search', 'web_search', 'fetch_url'),
    '媒体': ('generate_image', 'generate_video', 'comfyui_run'),
    '文件与代码': ('read_file', 'read_range', 'file_outline', 'list_dir', 'grep', 'find_files', 'write_file',
                  'edit_file', 'edit_config', 'apply_patch', 'delete_file', 'file_stats', 'file_export',
                  '7zip_extract', 'review_changes', 'git_status', 'git_log', 'git_add', 'git_commit'),
}
_CATEGORIES = {name: group for group, names in _GROUPS.items() for name in names}


class WorkToolSettings(QObject):
    changed = Signal()
    _loaded = Signal(str, object, str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.path = owner.store.folder / 'work-tools.json'
        stored = owner.store.read(self.path, {}) if not owner.demo else {}
        if not isinstance(stored, dict):
            stored = {}
        self._enabled = stored.get('enabled') is not False
        self._default = stored.get('default_enabled') is not False
        raw = stored.get('exceptions')
        self._exceptions = {name for name in raw if isinstance(name, str)} if isinstance(raw, list) else set()
        self._loading, self._error, self._save_error = False, '', ''
        self._gateway, self._local, self._rows = [], {}, []
        self._loaded.connect(self._receive)

    enabled = Property(bool, lambda self: self._enabled, notify=changed)
    loading = Property(bool, lambda self: self._loading, notify=changed)
    error = Property(str, lambda self: self._save_error or self._error, notify=changed)
    rows = Property('QVariantList', lambda self: self._rows, notify=changed)

    def snapshot(self):
        default, exceptions = self._default, frozenset(self._exceptions)
        profile = self.owner.tool_filter() if self.owner.tool_filter else lambda _: True

        def allowed(name):
            return profile(name) and (default != (name.removeprefix('forge_') in exceptions))

        return self._enabled and (default or bool(exceptions)), allowed

    def _save(self, enabled, default, exceptions):
        if self.owner.busy:
            return
        try:
            if not self.owner.demo:
                self.owner.store.atomic(self.path, {'enabled': enabled, 'default_enabled': default,
                                                     'exceptions': sorted(exceptions)})
        except OSError:
            self._save_error = '无法保存工具设置。'
            self.changed.emit()
            return
        self._save_error = ''
        self._enabled, self._default, self._exceptions = enabled, default, exceptions
        self._publish()

    @Slot(bool)
    def setEnabled(self, enabled):
        self._save(enabled, self._default, self._exceptions)

    @Slot(bool)
    def setAll(self, enabled):
        self._save(self._enabled, enabled, set())

    @Slot(str, bool)
    def setTool(self, name, enabled):
        if not any(row['id'] == name and row['available'] for row in self._rows):
            return
        key = name.removeprefix('forge_')
        exceptions = self._exceptions.copy()
        if enabled == self._default:
            exceptions.discard(key)
        else:
            exceptions.add(key)
        self._save(self._enabled, self._default, exceptions)

    @Slot()
    def refresh(self):
        if self._loading:
            return
        self._local = self.owner.mcp_tools()[0] if self.owner.mcp_tools else {}
        if not self.owner.connected or self.owner.demo:
            self._receive('', [], '网关未连接，仅显示本机工具。')
            return
        endpoint, factory = self.owner.gateway, self.owner.factory
        self._loading, self._error = True, ''
        self.changed.emit()

        def worker():
            try:
                rows = factory(endpoint).list_tools(timeout=5)
            except (RuntimeError, ValueError):
                self._loaded.emit(endpoint, [], '暂时无法读取网关工具。')
            else:
                self._loaded.emit(endpoint, rows, '')

        threading.Thread(target=worker, daemon=True, name='Forge-tool-catalog').start()

    @Slot(str, object, str)
    def _receive(self, endpoint, rows, error):
        if self.owner._closed:
            return
        self._loading = False
        if endpoint and endpoint != self.owner.gateway:
            self.refresh()
            return
        self._gateway, self._error = rows, error
        self._publish()

    def _publish(self):
        profile = self.owner.tool_filter() if self.owner.tool_filter else lambda _: True
        rows = []
        for schema in tool_schemas(self._gateway, self._local):
            function = schema['function']
            name = function['name']
            key = name.removeprefix('forge_')
            category = _CATEGORIES.get(key, '连接器' if name.startswith('mcp_') else '其他')
            rows.append({'id': name, 'label': tool_label(name), 'category': category,
                         'description': str(function.get('description') or '')[:500],
                         'available': profile(name), 'enabled': self._default != (key in self._exceptions)})
        self._rows = sorted(rows, key=lambda row: (not row['available'], row['category'], row['label']))
        self.changed.emit()
