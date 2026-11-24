"""Reply change cards; host receipts are the only source of undo authority."""
import threading
import uuid
from PySide6.QtCore import QObject, Property, Signal, Slot, Qt
from keystone.secrets import redact


class ReplyChanges(QObject):
    changed = Signal()
    openRequested = Signal()
    _result = Signal(str, object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.entries = {}
        self.active = ''
        self.selected = ''
        self._busy = False
        self._error = ''
        self._data = {'files': [], 'canRevert': False}
        self._result.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self.changed.connect(owner.changed.emit)

    busy = Property(bool, lambda self: self._busy, notify=changed)
    error = Property(str, lambda self: self._error, notify=changed)
    data = Property('QVariantMap', lambda self: self._data, notify=changed)

    def begin(self):
        self.active = uuid.uuid4().hex

    def record(self, name, response):
        meta = response.get('meta') or {}
        ids = meta.get('change_ids') or []
        files = meta.get('file_changes') or []
        if not ids and not files:
            return
        entry = self.entries.setdefault(self.active, {'ids': [], 'files': [],
            'endpoint': self.owner.gateway, 'anchor': len(self.owner._history)-1, 'canRevert': False})
        entry['ids'].extend(i for i in ids if isinstance(i, str) and i not in entry['ids'])
        entry['incomplete'] = entry.get('incomplete', False) or bool(meta.get('change_review_incomplete'))
        for value in files:
            if isinstance(value, dict):
                entry['files'].append({'name': value.get('path', ''), 'path': value.get('path', ''),
                    'added': value.get('added'), 'removed': value.get('removed'),
                    'diff': value.get('diff') or '', 'truncated': value.get('diff_truncated', False),
                    'kind': 'deleted' if meta.get('backup_path') else 'modified',
                    'backupPath': meta.get('backup_path', '')})

    def anchor(self):
        if self.active in self.entries:
            self.entries[self.active]['anchor'] = len(self.owner._history)-1

    def card(self, key):
        entry = self.entries.get(key, {})
        files = entry.get('files', [])
        return {'key': key, 'files': files, 'count': len(files),
            'countsKnown': all(f.get('added') is not None and f.get('removed') is not None for f in files),
            'added': sum(f.get('added') or 0 for f in files),
            'removed': sum(f.get('removed') or 0 for f in files),
            'restored': entry.get('restored', False),
            'canRevert': entry.get('canRevert', False),
            'unavailable': not entry.get('ids')}

    def saved(self, entries=None):
        # Raw backups never enter the conversation store or model context.
        entries = self.entries if entries is None else entries
        return {k: {**e, 'files': [{**f, 'diff': f.get('diff', '')[:16384]} for f in e.get('files', [])]}
                for k, e in entries.items()}

    def notice(self):
        files = [f.get('name', '') for e in self.entries.values()
                 if e.get('restored') and e.get('endpoint') == self.owner.gateway
                 for f in e.get('files', [])]
        if not files:
            return ''
        return ('界面操作记录：用户已通过回退按钮撤销以下文件的先前修改：'
                + ', '.join(files[-32:]) + '。旧工具回执只代表当时的状态；继续修改前请重新读取当前文件。')

    def finish(self):
        self.anchor()
        if self.active in self.entries:
            self._request(self.active)

    @Slot(str)
    def open(self, key):
        if self._busy or self.owner._busy or key not in self.entries:
            return
        self.selected = key
        self._error = ''
        self._data = {**self.entries[key], 'canRevert': False}
        self.openRequested.emit()
        self._request(key)

    @Slot()
    def revert(self):
        if self._busy or self.owner._busy or not self._data.get('canRevert') or not self.selected:
            return
        self._request(self.selected, revert=True)

    def _request(self, key, revert=False):
        entry = self.entries[key]
        if not entry.get('ids'):
            self._error = ('' if any(f.get('backupPath') for f in entry['files']) else
                '此记录没有回退快照，只能查看已有差异。更新并重启网关后，新文件修改会保存快照。')
            self.changed.emit()
            return
        self._busy, self._error = True, ''
        self.changed.emit()
        endpoint, ids = entry['endpoint'], list(entry['ids'])
        authorize = self.owner.tool_authorizer() if revert and self.owner.tool_authorizer else None
        files = list(self._data.get('files', []))
        def worker():
            try:
                if revert:
                    if authorize is None:
                        raise PermissionError('当前连接没有文件操作权限')
                    cancel = threading.Event()
                    for file in files:
                        authorize(file['undoTool'], {'path': file['path']}, cancel)
                result = self.owner.factory(endpoint).review_changes(ids, revert=revert)
                self._result.emit(key, (result, '', revert))
            except Exception as exc:
                self._result.emit(key, (None, str(redact(str(exc))), revert))
        threading.Thread(target=worker, daemon=True, name='Keystone-change-review').start()

    @Slot(str, object)
    def _receive(self, key, payload):
        if self.owner._closed:
            return
        result, error, reverting = payload
        self._busy = False
        self._error = error
        if error and reverting:
            self._error += ' 未自动重试；请重新查看差异确认文件状态。'
        entry = self.entries.get(key)
        if entry is not None:
            if result:
                previous_files = entry.get('files', [])
                entry.update({k: result[k] for k in ('files', 'canRevert', 'restored')})
                if entry.get('incomplete'):
                    known = {f['path'] for f in entry['files']}
                    entry['files'].extend(f for f in previous_files if f.get('backupPath') or f.get('path') not in known)
                    entry['canRevert'] = False
                    self._error = '本次包含无法安全保存快照的修改，暂不支持整次回退。'
            else:
                entry['canRevert'] = False
            if key == self.selected:
                self._data = dict(entry)
            for i, row in enumerate(self.owner._messages.rows):
                if row.get('changeKey') == key:
                    self.owner._messages.update(i, changeInfo=self.card(key))
            self.owner._persist()
        self.changed.emit()
        self.owner.streamAdvanced.emit()
