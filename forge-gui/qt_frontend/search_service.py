"""Public search shared by the desktop search panel and Work conversations."""
import json
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Property, Qt, Signal, Slot

from forge import public_search, web_backends
from forge.http_transport import open_response
from forge.secrets import redact
from forge.tools import _web_boundary

from .store import SessionStore


def search_public(query, engine, kind, count, cancel):
    timer_event = threading.Event()
    timer = threading.Timer(16, timer_event.set)
    timer.daemon = True

    class Cancel:
        def is_set(self):
            return cancel.is_set() or timer_event.is_set()

    try:
        query = public_search.clean_query(query)
        if engine not in public_search.SOURCES:
            raise ValueError('请选择支持的搜索源。')
        if redact(query) != query or 'SECRET_REF' in query:
            raise ValueError('搜索词不能包含 Forge 凭据。')
        url = public_search.search_url(engine, query, kind)
        timer.start()
        try:
            rows = web_backends.search(query=query, engine=engine, github_kind=kind, num=count,
                                       open_=lambda req, timeout: open_response(req, timeout=timeout,
                                                                                cancel_event=Cancel()))
        except RuntimeError as exc:
            message = '此来源暂时未返回可读取的结果。'
            if isinstance(exc, public_search.SearchUnavailable):
                message = str(exc)
            return {'ok': False, 'error': message, 'search_url': url, 'engine': engine}
        return {'ok': True, 'results': redact(rows), 'search_url': url, 'engine': engine}
    finally:
        timer.cancel()


class SearchService(QObject):
    changed = Signal()
    engineChanged = Signal()
    _completed = Signal(object, object)

    def __init__(self, home, parent=None, *, demo=False):
        super().__init__(parent)
        self.path = Path(home) / 'gui' / 'qt' / 'search.json'
        self.demo = demo
        settings = SessionStore.read(self.path, {})
        engine = settings.get('engine') if isinstance(settings, dict) else ''
        self._engine = engine if isinstance(engine, str) and engine in public_search.SOURCES else 'bing'
        self._cancel = None
        self._error, self._url, self._rows = '', '', []
        self._completed.connect(self._receive, Qt.ConnectionType.QueuedConnection)

    sources = Property('QVariantList', lambda s: [{'id': key, 'name': item[0]}
                       for key, item in public_search.SOURCES.items()], constant=True)
    engine = Property(str, lambda s: s._engine, notify=engineChanged)
    rows = Property('QVariantList', lambda s: s._rows, notify=changed)
    busy = Property(bool, lambda s: s._cancel is not None, notify=changed)
    error = Property(str, lambda s: s._error, notify=changed)
    sourceUrl = Property(str, lambda s: s._url, notify=changed)

    @Slot(str)
    def setEngine(self, engine):
        if engine not in public_search.SOURCES or engine == self._engine:
            return
        try:
            if not self.demo:
                SessionStore.atomic(self.path, {'engine': engine})
        except OSError:
            self._error = '无法保存默认搜索源。'
            self.changed.emit()
            return
        self._engine = engine
        self.engineChanged.emit()

    @Slot(str, str, str, result=str)
    def address(self, query, engine, kind):
        if engine not in public_search.SOURCES or redact(query) != query or 'SECRET_REF' in query:
            return ''
        try:
            return public_search.search_url(engine, query, kind)
        except ValueError:
            return ''

    @Slot()
    def reset(self):
        self.cancel()
        self._error, self._url, self._rows = '', '', []
        self.changed.emit()

    @Slot(str, str, str)
    def run(self, query, engine, kind):
        if self._cancel is not None:
            self._cancel.set()
        cancel = self._cancel = threading.Event()
        self._error, self._url, self._rows = '', '', []
        self.changed.emit()

        def worker():
            try:
                if self.demo:
                    raise ValueError('预览模式未发送联网搜索。')
                result = search_public(query, engine, kind, 8, cancel)
            except (ValueError, OSError) as exc:
                result = {'error': str(exc) if isinstance(exc, ValueError) else '无法连接搜索源。'}
            if not cancel.is_set():
                self._completed.emit(cancel, result)

        threading.Thread(target=worker, daemon=True, name='Forge-public-search').start()

    @Slot(object, object)
    def _receive(self, cancel, result):
        if self._cancel is not cancel:
            return
        self._cancel = None
        self._error = result.get('error', '')
        self._rows = result.get('results', [])
        self._url = result.get('search_url', '')
        self.changed.emit()

    @Slot()
    def cancel(self):
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None
            self.changed.emit()

    def work_tool(self):
        default_source = self._engine
        function = {
            'name': 'forge_public_search',
            'description': ('Search a single public source without credentials. Default source chosen by the user: '
                            + default_source + '. Cite real result URLs. If the source returns an error, report it '
                            'and offer the original search URL. GitHub only searches public repositories, '
                            'issues and pull requests.'),
            'parameters': {'type': 'object', 'required': ['query'], 'properties': {
                'query': {'type': 'string', 'description': 'Search terms only. Never include confidential data.'},
                'engine': {'type': 'string', 'enum': list(public_search.SOURCES)},
                'github_kind': {'type': 'string', 'enum': list(public_search.GITHUB_KINDS)},
                'num': {'type': 'integer', 'minimum': 1, 'maximum': 10},
            }},
        }
        return {'type': 'function', 'function': function}, default_source

    def call_work_tool(self, default_source, arguments, cancel):
        try:
            if self.demo:
                raise ValueError('预览模式未发送联网搜索。')
            query = arguments.get('query')
            engine = arguments.get('engine', default_source)
            kind = arguments.get('github_kind', 'repositories')
            count = arguments.get('num', 5)
            if (not isinstance(query, str) or not isinstance(engine, str) or not isinstance(kind, str)
                    or type(count) is not int or not 1 <= count <= 10):
                raise ValueError('搜索参数无效。')
            result = search_public(query, engine, kind, count, cancel)
            # Mark remote page text as untrusted, consistent with gateway web tools.
            rows = result.pop('results', None)
            if rows is not None:
                result['content'] = _web_boundary(engine + ' public search', json.dumps(rows, ensure_ascii=False))
            return {**result, 'meta': {'execution_state': 'completed'}}
        except (ValueError, OSError) as exc:
            return {'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else '无法连接搜索源。',
                    'meta': {'execution_state': 'not_started'}}
