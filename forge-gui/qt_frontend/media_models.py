"""User-owned media model profiles and deferred setup for a pending Work request."""
from contextlib import nullcontext
from pathlib import Path
import re
import threading
import uuid

from PySide6.QtCore import QObject, Property, Signal, Slot

import secret_store
from forge.runtime_lock import resource_lock
from forge.secrets import redact
from .local_tool_runtime import ToolFailure
from .media_generation import LOOPBACK, PRESETS, MediaGeneration, api_base, locator, prepare
from .store import SessionStore


def _credential(profile_id):
    return 'connector.media.' + profile_id


def _empty():
    return {'profiles': [], 'defaults': {}}


class MediaModels(QObject):
    changed = Signal()
    statusChanged = Signal()
    saved = Signal(str, bool)
    _requested = Signal(object)
    _cleared = Signal(object)
    _save_result = Signal(object)
    _progress = Signal(str)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        self.home = Path(home)
        self.path = self.home / 'gui' / 'qt' / 'media-models.json'
        self.credentials = None if self.home == Path.home() / '.forge' else self.home / 'secrets.json'
        self.demo = demo
        self._shutdown = threading.Event()
        self._pending = None
        self._saving = False
        self._notice, self._status = '', ''
        self._config = self._read()
        self._requested.connect(self._show_request)
        self._cleared.connect(self._clear_request)
        self._save_result.connect(self._finish_save)
        self._progress.connect(self._set_status)

    def _read(self):
        value = SessionStore.read(self.path, {})
        if (not isinstance(value, dict) or not isinstance(value.get('profiles'), list)
                or not isinstance(value.get('defaults'), dict)):
            return _empty()
        rows = [r for r in value['profiles'] if isinstance(r, dict)
                and isinstance(r.get('id'), str) and re.fullmatch(r'[a-f0-9]{32}', r['id'])
                and isinstance(r.get('protocol'), str) and r['protocol'] in PRESETS
                and isinstance(r.get('model'), str) and isinstance(r.get('url'), str)]
        return {'profiles': rows, 'defaults': value['defaults']}

    profiles = Property('QVariantList', lambda self: [
        {**row, 'kind': PRESETS[row['protocol']][0],
         'provider': PRESETS[row['protocol']][1], 'selected': row['id'] in self._config['defaults'].values()}
        for row in self._config['profiles']
    ], notify=changed)
    presets = Property('QVariantList', lambda self: [
        {'id': key, 'kind': kind, 'name': label, 'url': url, 'example': model}
        for key, (kind, label, url, model) in PRESETS.items()
    ], constant=True)
    requestKind = Property(str, lambda self: self._pending['kind'] if self._pending else '', notify=changed)
    saving = Property(bool, lambda self: self._saving, notify=changed)
    notice = Property(str, lambda self: self._notice, notify=changed)
    status = Property(str, lambda self: self._status, notify=statusChanged)

    @Slot()
    def clearNotice(self):
        if self._notice:
            self._notice = ''
            self.changed.emit()

    @Slot(str, str, str, str, str, bool)
    def saveProfile(self, profile_id, protocol, url, model, key, default):
        if self._saving:
            return
        pending = self._pending
        if self.demo or protocol not in PRESETS or (pending and PRESETS[protocol][0] != pending['kind']):
            self._notice = '请选择与当前生成任务一致的模型类型。'
            self.changed.emit()
            return
        self._notice, self._saving = '', True
        self.changed.emit()

        def worker():
            try:
                row = self._save(profile_id, protocol, url, model, key, default or bool(pending))
                result = {'row': row, 'pending': pending}
            except (OSError, ValueError) as exc:
                result = {'error': str(redact(str(exc))) if isinstance(exc, ValueError)
                          else '无法保存模型配置或密钥，请检查本机配置。'}
            self._save_result.emit(result)

        threading.Thread(target=worker, daemon=True, name='Forge-media-profile').start()

    def _save(self, profile_id, protocol, url, model, key, is_default):
        model, key = model.strip(), key.strip()
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', model) or model in {'.', '..'}:
            raise ValueError('请输入厂商控制台中的模型 ID。')
        if protocol == 'flux' and not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', model):
            raise ValueError('FLUX 模型 ID 应填写 API 端点名称，例如 flux-2-pro-preview。')
        if len(key) > 4096 or any(ch.isspace() or ord(ch) < 32 for ch in key):
            raise ValueError('密钥格式无效。')
        address = api_base(url, protocol, model)
        endpoint, _ = locator(address)
        kind = PRESETS[protocol][0]
        with resource_lock('media-models', self.path):
            settings = self._read()
            profiles = settings['profiles']
            old = next((r for r in profiles if r['id'] == profile_id), None)
            if profile_id and (old is None or PRESETS[old['protocol']][0] != kind):
                raise ValueError('原模型已变更，请重新打开配置。')
            if not profile_id and len(profiles) >= 64:
                raise ValueError('已达到模型配置数量上限。')
            profile_id = profile_id or uuid.uuid4().hex
            name = _credential(profile_id)
            saved = secret_store.load(strict=True, path=self.credentials).get(name, '') if old else ''
            same_endpoint = old is not None and (old['protocol'], old['url']) == (protocol, address)
            if not key and same_endpoint:
                key = saved
            if not key and endpoint.hostname not in LOOPBACK:
                raise ValueError('请输入此厂商接口的 API 密钥。')
            row = {'id': profile_id, 'protocol': protocol, 'url': address, 'model': model}
            settings['profiles'] = [row if r['id'] == profile_id else r for r in profiles] if old else [*profiles, row]
            if is_default or kind not in settings['defaults']:
                settings['defaults'][kind] = profile_id
            transaction = (secret_store.update_provider(name, key, path=self.credentials)
                           if key or saved else nullcontext())
            with transaction:
                SessionStore.atomic(self.path, settings)
        return row

    @Slot(object)
    def _finish_save(self, result):
        self._saving = False
        self._notice = result.get('error', '')
        row = result.get('row')
        if row:
            self._config = self._read()
            self._notice = '已保存模型配置。'
            request = result['pending']
            resumed = request is not None and request is self._pending
            if resumed:
                request['profile_id'] = row['id']
                self._clear_request(request)
                request['done'].set()
            self.changed.emit()
            self.saved.emit(row['id'], resumed)
        else:
            self.changed.emit()

    def _profile(self, kind, profile_id=''):
        with resource_lock('media-models', self.path):
            settings = self._read()
            selected = profile_id or settings['defaults'].get(kind)
            row = next((r for r in settings['profiles']
                        if r['id'] == selected and PRESETS[r['protocol']][0] == kind), None)
            if row is None:
                return None, ''
            address = api_base(row['url'], row['protocol'], row['model'])
            endpoint, _ = locator(address)
            key = secret_store.load(strict=True, path=self.credentials).get(_credential(selected), '')
            return ({**row, 'url': address}, key) if key or endpoint.hostname in LOOPBACK else (None, '')

    def _wait_for_setup(self, kind, cancel):
        request = {'kind': kind, 'done': threading.Event(), 'profile_id': ''}
        self._requested.emit(request)
        while not request['done'].wait(.1):
            if cancel.is_set() or self._shutdown.is_set():
                self._cleared.emit(request)
                return ''
        return request['profile_id'] if not (cancel.is_set() or self._shutdown.is_set()) else ''

    @Slot(object)
    def _show_request(self, request):
        if self._shutdown.is_set() or self._pending is not None:
            request['done'].set()
            return
        self._pending, self._notice = request, ''
        self._config = self._read()
        self.changed.emit()

    @Slot(object)
    def _clear_request(self, request):
        if self._pending is request:
            self._pending = None
            self.changed.emit()

    @Slot()
    def cancelRequest(self):
        if self._pending:
            request = self._pending
            self._clear_request(request)
            request['done'].set()

    @Slot(str)
    def _set_status(self, text):
        self._status = text
        self.statusChanged.emit()

    def call(self, kind, workspace, args, cancel):
        try:
            if self.demo or self._shutdown.is_set():
                raise ToolFailure('预览模式不会提交生成请求。')
            prompt, destination = prepare(kind, workspace, args)
            row, key = self._profile(kind)
            if row is None:
                selected = self._wait_for_setup(kind, cancel)
                if not selected:
                    return {'ok': False, 'error': '未配置生成模型，本次生成已跳过。',
                            'meta': {'execution_state': 'not_started', 'error_code': 'media_setup_skipped'}}
                row, key = self._profile(kind, selected)
                if row is None:
                    raise ToolFailure('模型配置不可用，请在模型配置页检查。')
            return MediaGeneration(row, key, cancel, self._progress.emit).run(workspace, prompt, destination)
        except ToolFailure as exc:
            return {'ok': False, 'error': str(exc), **exc.details, 'meta': {'execution_state': exc.state}}
        except (OSError, ValueError):
            return {'ok': False, 'error': '模型配置不可读，请在模型配置页检查。',
                    'meta': {'execution_state': 'not_started'}}
        finally:
            self._progress.emit('')

    def shutdown(self):
        self._shutdown.set()
        self.cancelRequest()
