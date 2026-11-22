"""One Qt shell, one private child host, and a retained browser side panel."""
import json
from pathlib import Path
import re
import sys
import threading
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Property, QProcess, QProcessEnvironment, QTimer, Signal, Slot
from keystone.desktop_permissions import LABELS, PROFILES
from keystone.secrets import redact
from .controller import RowsModel
from brand_marks import mark_path
import provider_catalog
from .mcp_catalog import McpCatalog
from .search_service import SearchService
from .wallpaper import Wallpaper
from .onboarding import Onboarding
from .model_rows import ModelRows
from .local_tools import LocalTools
from .media_models import MediaModels

PAGES = {'chat': 'Work 对话', 'development': 'Coding', 'task': '任务', 'files': '文件与项目',
         'tools': '工具与插件', 'config': '模型与配置', 'agents': 'Agents', 'knowledge': '知识库',
         'evolution': '演化', 'schedules': '定时任务', 'connectors': '连接器',
         'preview': '代码预览', 'computer': '电脑操作', 'work': 'Work 完整功能'}
PAGES['connector_accounts'] = '连接器账户'


class WorkbenchController(QObject):
    changed = Signal()
    browserChanged = Signal()
    permissionRequested = Signal()
    gatewayChanged = Signal(str, object)
    gatewayChanging = Signal()
    gatewayCatalog = Signal(object)
    gatewayFailed = Signal(str, str)
    codingChanged = Signal()
    codingDraftChanged = Signal()
    codingFocusRequested = Signal()
    terminalChanged = Signal()
    terminalTabsChanged = Signal()
    terminalRevealRequested = Signal()
    pageReadyChanged = Signal()
    catalogRequested = Signal(str)
    modelSetupRequested = Signal()
    modelEditRequested = Signal(str)
    pagesChanged = Signal()
    workspaceQueryResult = Signal('QVariantMap')
    formReset = Signal(str)
    formValues = Signal(str, 'QVariantMap')
    historyRequested = Signal('QVariantList')
    _serviceCommand = Signal(object)
    _approvalRequested = Signal(str, str, str)
    _approvalFinished = Signal(str)

    def __init__(self, home, *, demo=False, parent=None):
        super().__init__(parent)
        self.home, self.demo = Path(home), demo
        self._mcp = McpCatalog(self.home, self, demo=demo)
        self._search = SearchService(self.home, self, demo=demo)
        self._wallpaper = Wallpaper(self.home, self)
        self._local_tools = LocalTools(self.home, self, demo=demo)
        self._media_models = MediaModels(self.home, self, demo=demo)
        self._onboarding = Onboarding(self.home, self, demo=demo)
        self._onboarding.can_save = lambda: not (self._busy or self.hasUnsaved or self._pending or self._input_busy())
        self._onboarding.saved.connect(self._apply_model_setup)
        self._onboarding.preferencesChanged.connect(self._apply_onboarding_preferences)
        self._model_rows = ModelRows(self.home, self, demo=demo)
        self._model_rows.can_save = self._onboarding.can_save
        self._model_rows.saved.connect(self._apply_model_edit)
        self._model_setup_pending = None
        self._page, self._profile = 'chat', 'sandbox'
        self._navigation = False
        self._browser_visible, self._browser_loaded = False, False
        self._browser_expanded = False
        self._before_files = 'chat'
        self._browser_url, self._browser_width = 'about:blank', 420.0
        self._window = None
        self._service_ready = False
        self._pages = {}
        self._form_dirty = set()
        self._document_dirty = set()
        self._document_versions = {}
        self._service_tools = []
        self._computer_grant = ''
        self._service_calls = {}
        self._serviceCommand.connect(self._send)
        self._coding = {}
        self._terminal_serial = 0
        self._terminals = {}
        self._terminal_output = ''
        self._terminal_size = [100, 24]
        self._add_terminal()
        self._terminal_timer = QTimer(self)
        self._terminal_timer.setSingleShot(True)
        self._terminal_timer.setInterval(50)
        self._terminal_timer.timeout.connect(self.terminalChanged)
        self._coding_messages = RowsModel(parent=self)
        self._coding_draft = ''
        self._draft_version = 0
        self._error, self._status = '', ''
        self._busy = False
        self._unsaved = False
        self._page_serial = 0
        self._navigation_ticket = 0
        self._native_ready = False
        self._pending_plan = None
        self._pending_account = ''
        self._providers = [
            {'key': preset.key, 'name': preset.name, 'source': preset.source_label,
             'search': ' '.join((preset.key, preset.name, *preset.aliases,
                                 *(plan.base_url for plan in preset.plans))).lower(),
             'icon': icon.as_uri() if (icon := mark_path(preset.brand, 32)) else '',
             'plans': [{'label': plan.label, 'url': plan.base_url, 'wire': plan.wire,
                        'usable': plan.usable, 'note': plan.note} for plan in preset.plans]}
            for preset in provider_catalog.PRESETS
        ]
        self._workspace = str(Path(__file__).resolve().parents[2])
        self._pending = []
        self._waiters = {}
        self._input_busy = lambda: False
        self._buffer = b''
        self._closing = False
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.readyReadStandardError.connect(self._read_error)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._approvalRequested.connect(self._queue_local_approval)
        self._approvalFinished.connect(self._clear_approval)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._host_timeout)

    page = Property(str, lambda s: s._page, notify=changed)
    pageTitle = Property(str, lambda s: PAGES[s._page], notify=changed)
    navigationOpen = Property(bool, lambda s: s._navigation, notify=changed)
    nativeWindow = Property(QObject, lambda s: s._window, notify=changed)
    nativePageReady = Property(bool, lambda s: s._native_ready, notify=pageReadyChanged)
    providers = Property('QVariantList', lambda s: s._providers, constant=True)
    mcp = Property(QObject, lambda s: s._mcp, constant=True)
    search = Property(QObject, lambda s: s._search, constant=True)
    wallpaper = Property(QObject, lambda s: s._wallpaper, constant=True)
    localTools = Property(QObject, lambda s: s._local_tools, constant=True)
    mediaModels = Property(QObject, lambda s: s._media_models, constant=True)
    onboarding = Property(QObject, lambda s: s._onboarding, constant=True)
    modelRows = Property(QObject, lambda s: s._model_rows, constant=True)
    busy = Property(bool, lambda s: s._busy, notify=changed)
    hasUnsaved = Property(bool, lambda s: s._unsaved or bool(s._form_dirty or s._document_dirty), notify=changed)
    pageData = Property('QVariantMap', lambda s: s._pages, notify=pagesChanged)
    error = Property(str, lambda s: s._error, notify=changed)
    status = Property(str, lambda s: s._status, notify=changed)
    workspace = Property(str, lambda s: s._workspace, notify=changed)
    permission = Property(str, lambda s: s._profile, notify=changed)
    permissionLabel = Property(str, lambda s: LABELS[s._profile], notify=changed)
    approvals = Property('QVariantList', lambda s: s._pending, notify=changed)
    browserVisible = Property(bool, lambda s: s._browser_visible, notify=browserChanged)
    browserExpanded = Property(bool, lambda s: s._browser_expanded, notify=browserChanged)
    browserLoaded = Property(bool, lambda s: s._browser_loaded, notify=browserChanged)
    browserUrl = Property(str, lambda s: s._browser_url, notify=browserChanged)
    browserWidth = Property(float, lambda s: s._browser_width, notify=browserChanged)
    terminalText = Property(str, lambda s: s._terminals[s._terminal_id]['log'] + s._terminals[s._terminal_id]['screen'], notify=terminalChanged)
    terminalOutput = Property(str, lambda s: s._terminal_output, notify=terminalChanged)
    terminalRunning = Property(bool, lambda s: any(tab['running'] for tab in s._terminals.values()), notify=terminalChanged)
    activeTerminalRunning = Property(bool, lambda s: s._terminals[s._terminal_id]['running'], notify=terminalChanged)
    activeTerminal = Property(int, lambda s: s._terminal_id, notify=terminalTabsChanged)
    terminalTabs = Property('QVariantList', lambda s: list(s._terminals), notify=terminalTabsChanged)
    coding = Property('QVariantMap', lambda s: s._coding, notify=codingChanged)
    codingMessages = Property(QObject, lambda s: s._coding_messages, constant=True)
    codingDraft = Property(str, lambda s: s._coding_draft, notify=codingDraftChanged)

    def service_tools(self):
        tools = {}
        for schema in self._service_tools:
            name = schema['function']['name']
            grant = self._computer_grant if name.startswith('computer_') else ''
            tools[name] = (schema, ('service', name, grant))
        return tools

    def call_service(self, name, arguments, cancel, grant=''):
        identifier = uuid.uuid4().hex
        waiter = {'done': threading.Event(), 'result': None}
        self._service_calls[identifier] = waiter
        self._serviceCommand.emit({'command': 'call_tool', 'id': identifier, 'name': name,
                                   'arguments': arguments, 'computerGrant': grant})
        try:
            while not waiter['done'].wait(.1):
                if self._closing or cancel.is_set():
                    self._serviceCommand.emit({'command': 'cancel_call', 'id': identifier})
                    return {'ok': False, 'error': '操作已取消', 'meta': {'execution_state': 'unknown'}}
            return waiter['result'] or {'ok': False, 'error': '后台服务已停止'}
        finally:
            self._service_calls.pop(identifier, None)

    @Slot(str, str, str)
    def pageAction(self, page, action, payload):
        try:
            values = json.loads(payload)
            if not isinstance(values, dict):
                raise ValueError('页面参数必须是对象')
        except (ValueError, TypeError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return
        if page == 'computer' and action == 'settings':
            if values.get('changed') == 'model':
                self.formValues.emit(page, {'consent': False})
            self._send({'command': 'computer_settings'})
            return
        self._send({'command': 'page_action', 'page': page, 'action': action, 'values': values})

    @Slot(str, str, int, bool)
    def queryWorkspace(self, kind, query, ticket, cancel):
        self._send({'command': 'workspace_query', 'kind': kind, 'query': query, 'ticket': ticket, 'cancel': cancel})

    def _add_terminal(self):
        self._terminal_serial += 1
        self._terminal_id = self._terminal_serial
        self._terminals[self._terminal_id] = {'log': '', 'screen': '', 'running': False}

    def _publish_tabs(self):
        self.terminalTabsChanged.emit()
        self.terminalChanged.emit()

    @Slot(int)
    def selectTerminal(self, identifier):
        if identifier in self._terminals and self._terminal_id != identifier:
            self._terminal_id = identifier
            self._publish_tabs()
            self.terminalAction('resize', '')

    @Slot(int, int)
    def resizeTerminal(self, columns, rows):
        if self._terminal_size != [columns, rows]:
            self._terminal_size = [columns, rows]
            self.terminalAction('resize', '')

    @Slot(str, str)
    def terminalAction(self, action, text):
        if self._page != 'development' or not self._native_ready:
            return
        if action == 'new':
            if len(self._terminals) < 16:
                self._add_terminal()
                self._publish_tabs()
            return
        self._send({'command': 'terminal', 'action': action, 'text': text, 'session': self._terminal_id,
                    'size': self._terminal_size, 'root': self._workspace})
        if action == 'clear':
            self._terminals[self._terminal_id].update(log='', screen='')
            self.terminalChanged.emit()
        elif action == 'close':
            position = list(self._terminals).index(self._terminal_id)
            del self._terminals[self._terminal_id]
            if self._terminals:
                self._terminal_id = list(self._terminals)[min(position, len(self._terminals) - 1)]
            else:
                self._add_terminal()
            self._publish_tabs()

    def _terminal_event(self, row):
        text = row['text'].replace('\r\n', '\n')
        session = self._terminals.get(row.get('session'))
        if session:
            if row.get('cleared'):
                session.update(log='', screen='')
            if row.get('ended') and session['screen']:
                session['log'] += session['screen'] + '\n'
                session['screen'] = ''
            session['log'] = (session['log'] + text)[-131072:]
            if 'screen' in row:
                session['screen'] = row['screen']
            if 'running' in row:
                session['running'] = row['running']
        elif 'session' not in row:
            self._terminal_output = (self._terminal_output + text)[-131072:]
            if row.get('reveal'):
                self.terminalRevealRequested.emit()
        else:
            return
        if 'running' in row:
            self._terminal_timer.stop()
            self.terminalChanged.emit()
        elif not self._terminal_timer.isActive():
            self._terminal_timer.start()

    @Slot(str, bool)
    def setFormDirty(self, page, dirty):
        (self._form_dirty.add if dirty else self._form_dirty.discard)(page)
        self.changed.emit()

    @Slot(str, str, int, bool)
    def updateDocument(self, path, text, version, dirty):
        (self._document_dirty.add if dirty else self._document_dirty.discard)(path)
        self._document_versions[path] = version
        self._send({'command': 'document', 'path': path, 'text': text, 'version': version})
        self.changed.emit()

    @Slot(str)
    def setCodingDraft(self, text):
        if text == self._coding_draft:
            return
        self._coding_draft = text
        self._draft_version += 1
        if self._coding.get('ready'):
            self._send_coding_draft()
        self.codingDraftChanged.emit()

    @Slot(str, str)
    def codingAction(self, action, value):
        if self._page != 'development' or not self._coding.get('ready'):
            return
        self._send_coding_draft()
        self._send({'command': 'coding', 'action': action, 'value': value})

    def _send_coding_draft(self):
        self._send({'command': 'coding', 'action': 'draft', 'value': self._coding_draft,
                    'version': self._draft_version})

    @Slot()
    def toggleNavigation(self):
        self._navigation = not self._navigation
        self.changed.emit()

    @Slot()
    def clearError(self):
        self._error = ''
        self.changed.emit()

    @Slot(str)
    def navigate(self, page):
        if page == 'work':
            page = 'chat'
        if page not in PAGES or (not self._onboarding.developer and page in {'development', 'evolution'}):
            return
        if page == self._page and (self._native_ready or page in {'chat', 'connectors'}):
            return
        if page == 'files' and self._page != 'files':
            self._before_files = self._page
        self._navigation_ticket += 1
        self._set_native_ready(False)
        self._error = ''
        self._page = page
        if page == 'connectors':
            self._mcp.load()
        if page != 'config':
            self._pending_plan = None
        if page != 'connector_accounts':
            self._pending_account = ''
        self._navigation = page != 'chat'
        if page not in {'chat', 'connectors'} and self._process.state() == QProcess.ProcessState.NotRunning:
            self.start_host()
        self._send_navigation()
        self.changed.emit()

    def _set_native_ready(self, ready):
        if ready != self._native_ready:
            self._native_ready = ready
            self.pageReadyChanged.emit()

    def _send_navigation(self):
        if self._service_ready:
            self._send({'command': 'navigate', 'page': self._page, 'ticket': self._navigation_ticket})
        elif self._page in {'chat', 'connectors'}:
            self._page_serial += 1

    @Slot(str)
    def showProviderCatalog(self, query):
        self.catalogRequested.emit(query)

    @Slot(str, str)
    def useProviderPlan(self, provider, plan):
        self._pending_plan = (provider, plan)
        self.navigate('config')
        self._fill_provider_plan()

    def _fill_provider_plan(self):
        if self._page == 'config' and self._native_ready and self._pending_plan:
            provider, plan = self._pending_plan
            self._pending_plan = None
            self._send({'command': 'provider_plan', 'provider': provider, 'plan': plan})

    @Slot(str)
    def showConnectorAccounts(self, account):
        self._pending_account = account if account in {'github', 'microsoft'} else ''
        self.navigate('connector_accounts')
        self._select_connector_account()

    def _select_connector_account(self):
        if self._page == 'connector_accounts' and self._native_ready and self._pending_account:
            self._send({'command': 'connector_account', 'account': self._pending_account})
            self._pending_account = ''

    def _apply_onboarding_preferences(self):
        if not self._onboarding.developer and self._page in {'development', 'evolution'}:
            self.navigate('chat')
        if self._service_ready:
            self._send({'command': 'onboarding_preferences', **self._onboarding.preferences()})

    def _apply_model_setup(self, row_id):
        self.gatewayChanging.emit()
        self._request_gateway({'command': 'apply_model_setup', 'provider': row_id})

    @Slot(str)
    def editModel(self, row_id):
        self._model_rows.reload()
        self.modelEditRequested.emit(row_id)

    def _apply_model_edit(self, _row_id):
        # Restart on the saved chat choice; an edited label falls back like any removed model.
        self.gatewayChanging.emit()
        self._request_gateway({'command': 'apply_model_setup'})
        if self._page == 'config' and self._service_ready:
            self._send({'command': 'page_action', 'page': 'config', 'action': 'reload', 'values': {}})

    @Slot(str)
    def switchChatModel(self, label):
        self._request_gateway({'command': 'apply_model_setup', 'model': label})

    def _request_gateway(self, request):
        if not self._service_ready:
            self._model_setup_pending = request
            self.start_host()
        else:
            self._send(request)

    @Slot()
    def start_host(self):
        if self._process.state() != QProcess.ProcessState.NotRunning:
            return
        self._error, self._status = '', '正在载入工作台…'
        gui = Path(__file__).resolve().parents[1]
        executable = Path(sys.executable).with_name('python.exe')
        self._process.setProgram(str(executable))
        args = [str(gui / 'qt_frontend' / 'service_host.py'), '--home', str(self.home)]
        if self.demo:
            args.append('--demo')
        self._process.setArguments(args)
        self._process.setWorkingDirectory(str(gui))
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONIOENCODING', 'utf-8')
        env.insert('KEYSTONE_NO_AUTOSTART', '1' if self.demo else '0')
        self._process.setProcessEnvironment(env)
        self._process.start()
        self._timeout.start(30000)
        self.changed.emit()

    def _send(self, value):
        if self._process.state() == QProcess.ProcessState.Running:
            self._process.write((json.dumps(value, ensure_ascii=False) + '\n').encode())

    def _read(self):
        self._buffer += bytes(self._process.readAllStandardOutput())
        while b'\n' in self._buffer:
            raw, self._buffer = self._buffer.split(b'\n', 1)
            if not raw.startswith(b'KEYSTONE_UI:'):
                continue
            try:
                row = json.loads(raw[len(b'KEYSTONE_UI:'):])
                self._receive(row)
            except (ValueError, KeyError, TypeError) as exc:
                self._error = '工作台通信错误：' + str(exc)
                self.changed.emit()

    def _receive(self, row):
        kind = row.get('event')
        if kind == 'ready':
            self._timeout.stop()
            self._service_ready = True
            self._status = ''
            self.changed.emit()
            self._send({'command': 'permission', 'profile': self._profile})
            self._apply_onboarding_preferences()
            if self._model_setup_pending:
                request, self._model_setup_pending = self._model_setup_pending, None
                self._send(request)
            self._send_navigation()
        elif kind == 'page':
            self._pages[row['page']] = row['data']
            document = row['data'].get('document') or {}
            if row['page'] in {'files', 'development'}:
                self._document_dirty.intersection_update(t['path'] for t in row['data'].get('tabs', []))
            if document and not document.get('dirty') and document.get('version', 0) >= self._document_versions.get(document['path'], 0):
                self._document_dirty.discard(document['path'])
            self.pagesChanged.emit()
            self.changed.emit()
        elif kind == 'terminal' and row['root'] == self._workspace:
            self._terminal_event(row)
        elif kind == 'workspace_query':
            self.workspaceQueryResult.emit(row)
        elif kind == 'reset_form':
            self._form_dirty.discard(row['page'])
            self.formReset.emit(row['page'])
        elif kind == 'form_values':
            self.formValues.emit(row['page'], row['values'])
        elif kind == 'saved':
            self._form_dirty.discard(row['page'])
            self.changed.emit()
        elif kind == 'model_setup':
            self.modelSetupRequested.emit()
        elif kind == 'history':
            self.historyRequested.emit(row['rows'])
        elif kind == 'service_tools':
            self._computer_grant = row.get('computerGrant', '')
            self._service_tools = row['schemas']
        elif kind == 'call_result':
            waiter = self._service_calls.get(row['id'])
            if waiter:
                waiter['result'] = row['result']
                waiter['done'].set()
        elif kind == 'coding_state':
            first = not self._coding.get('ready')
            self._coding = {**self._coding, **row['state']}
            if first and self._coding_draft:
                self._send_coding_draft()
            self.codingChanged.emit()
        elif kind == 'coding_message':
            message = row['message']
            if message is None:
                self._coding_messages.reset([])
            else:
                self._coding_messages.append({'roleName': message['role'], 'textContent': message['body'],
                                              'stamp': message['stamp']})
        elif kind == 'coding_draft':
            if row['version'] == self._draft_version and row['text'] != self._coding_draft:
                self._coding_draft = row['text']
                self.codingDraftChanged.emit()
        elif kind == 'coding_focus':
            self.codingFocusRequested.emit()
        elif kind == 'status':
            self._busy = bool(row.get('busy'))
            self._unsaved = bool(row.get('unsaved'))
            workspace = row.get('workspace', self._workspace)
            if workspace != self._workspace:
                self._workspace = workspace
                self._terminals.clear()
                self._terminal_output = ''
                self._add_terminal()
                self._publish_tabs()
            self._status = row.get('status', '')
            self.changed.emit()
        elif kind == 'navigate':
            if row.get('ticket') is not None and row['ticket'] != self._navigation_ticket:
                return
            if row.get('page') in PAGES:
                self._page = row['page']
                self._page_serial += 1
                self._navigation = self._page != 'chat'
                self._set_native_ready(self._page not in {'chat', 'connectors'})
                self._fill_provider_plan()
                self._select_connector_account()
                self.changed.emit()
        elif kind == 'provider_catalog':
            self.catalogRequested.emit(str(row.get('query', '')))
        elif kind == 'navigation_error':
            if row.get('ticket') is None or row['ticket'] == self._navigation_ticket:
                self._error = str(row.get('message', '页面载入失败'))
                self.changed.emit()
        elif kind == 'browser':
            self.openBrowser(str(row.get('url', '')))
        elif kind == 'collapse_files' and self._page == 'files':
            self.toggleFiles()
        elif kind == 'permissions':
            self.permissionRequested.emit()
        elif kind == 'approval':
            self._pending.append({'id': row['id'], 'title': row['title'], 'detail': row['detail'], 'source': 'host'})
            self.changed.emit()
        elif kind == 'approval_closed':
            self._pending = [r for r in self._pending if r['id'] != row['id']]
            self.changed.emit()
        elif kind == 'permission':
            if row.get('profile') in PROFILES:
                self._profile = row['profile']
            self.changed.emit()
        elif kind == 'gateway':
            self.gatewayChanged.emit(row['url'], {'temperature': row.get('temperature'), 'model': row.get('model')})
        elif kind == 'gateway_catalog':
            self.gatewayCatalog.emit(row['models'])
        elif kind == 'gateway_error':
            self.gatewayFailed.emit(row['message'], row.get('active_url', ''))
        elif kind == 'error':
            self._error = str(row.get('message', '工作台载入失败'))
            self.changed.emit()
        elif kind == 'closing' and not self._closing:
            self._error = '工作台退出：' + str(row.get('reason', ''))
            self.changed.emit()
        elif kind == 'host_stopped' and not row.get('closed') and not self._closing:
            self._error = '功能窗口意外结束。'
            self.changed.emit()

    def _read_error(self):
        text = bytes(self._process.readAllStandardError()).decode('utf-8', 'replace')
        if 'Traceback' in text or 'Error:' in text:
            self._error = str(redact(text[-2000:]))
            self.changed.emit()

    def _process_error(self, _):
        if not self._closing:
            self._error = '工作台启动失败：' + self._process.errorString()
            self._model_setup_pending = None
            self.gatewayFailed.emit(self._error, '')
            self.changed.emit()

    def _host_timeout(self):
        if not self._service_ready:
            self._error = '工作台仍未就绪。可返回对话，或关闭后重新打开。'
            self._model_setup_pending = None
            self.gatewayFailed.emit(self._error, '')
            self.changed.emit()

    def _finished(self, exit_code, exit_status):
        self._timeout.stop()
        self._set_native_ready(False)
        self._service_ready = False
        self._service_tools = []
        for waiter in self._service_calls.values():
            waiter['done'].set()
        self._busy = False
        for session in self._terminals.values():
            if session['running']:
                session['log'] = (session['log'] + session['screen'] + '\n[终端连接已关闭]\n')[-131072:]
                session.update(screen='', running=False)
        self.terminalChanged.emit()
        self._coding = {}
        self._coding_messages.reset([])
        self.codingChanged.emit()
        self._pending = [r for r in self._pending if r['source'] != 'host']
        if not self._closing:
            self._error = self._error or f'功能工作台已关闭（退出码 {exit_code}），可点击重新载入。'
            self._model_setup_pending = None
            self.gatewayFailed.emit(self._error, '')
        self.changed.emit()

    @Slot(str)
    def setPermission(self, profile):
        if profile not in PROFILES or self._busy or self._input_busy() or self._pending:
            return
        if self._process.state() == QProcess.ProcessState.Running:
            self._send({'command': 'permission', 'profile': profile})
        else:
            self._profile = profile
            self.changed.emit()

    def approve(self, title, args, cancel=None):
        identifier = uuid.uuid4().hex
        done = threading.Event()
        waiter = {'done': done, 'approved': False}
        self._waiters[identifier] = waiter
        self._approvalRequested.emit(identifier, title, json.dumps(redact(args), ensure_ascii=False, indent=2))
        while not done.wait(0.1):
            if self._closing or cancel is not None and cancel.is_set():
                break
        self._waiters.pop(identifier, None)
        self._approvalFinished.emit(identifier)
        return waiter['approved'] and not self._closing and not (cancel is not None and cancel.is_set())

    @Slot(str)
    def _clear_approval(self, identifier):
        self._pending = [row for row in self._pending if row['id'] != identifier]
        self.changed.emit()

    @Slot(str, str, str)
    def _queue_local_approval(self, identifier, title, detail):
        if identifier in self._waiters:
            self._pending.append({'id': identifier, 'title': title, 'detail': detail, 'source': 'local'})
            self.changed.emit()

    @Slot(str, bool)
    def resolveApproval(self, identifier, approved):
        row = next((r for r in self._pending if r['id'] == identifier), None)
        if row is None:
            return
        self._pending.remove(row)
        if row['source'] == 'host':
            self._send({'command': 'approval', 'id': identifier, 'approved': approved})
        else:
            waiter = self._waiters.get(identifier)
            if waiter:
                waiter['approved'] = approved
                waiter['done'].set()
        self.changed.emit()

    @Slot()
    def toggleFiles(self):
        self.navigate(self._before_files if self._page == 'files' else 'files')

    @Slot()
    def toggleBrowser(self):
        self._browser_visible = not self._browser_visible
        self._browser_loaded = self._browser_loaded or self._browser_visible
        self.browserChanged.emit()

    @Slot()
    def toggleBrowserWidth(self):
        self._browser_expanded = not self._browser_expanded
        self.browserChanged.emit()

    @Slot(float)
    def resizeBrowser(self, width):
        self._browser_width = max(300, min(720, width))
        self.browserChanged.emit()

    @Slot(str)
    def openBrowserAddress(self, value):
        value = value.strip()
        if not value:
            return
        # Treat domain-like values as addresses and send ordinary text to the selected search source.
        host = re.match(r'^(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+|\[[a-fA-F0-9:]+\])'
                        r'(?::[0-9]{1,5})?(?:[/?#]|$)', value, re.I)
        explicit_scheme = re.match(r'^[a-z][a-z0-9+.-]*://', value, re.I)
        if explicit_scheme or value.startswith(('about:', 'javascript:', 'file:', 'data:')) or (
                host and not any(c.isspace() for c in value)):
            self.openBrowser(value)
            return
        target = self._search.address(value, self._search.engine, 'repositories')
        if target:
            self.openBrowser(target)
        else:
            self._error = '请输入有效的公开搜索词。'
            self.changed.emit()

    @Slot(str)
    def openBrowser(self, value):
        value = value.strip()
        if value and '://' not in value and not value.startswith(('about:', 'javascript:', 'file:', 'data:')):
            value = 'https://' + value
        try:
            parsed = urlsplit(value)
        except ValueError:
            self._error = '网址格式不正确。'
            self.changed.emit()
            return
        if value != 'about:blank' and (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password):
            self._error = '浏览器支持 HTTP / HTTPS 地址。'
            self.changed.emit()
            return
        self._browser_url = value or 'about:blank'
        self._browser_visible = self._browser_loaded = True
        self.browserChanged.emit()

    def shutdown(self):
        self._closing = True
        self._mcp.shutdown()
        self._local_tools.shutdown()
        self._media_models.shutdown()
        self._search.cancel()
        self._wallpaper.shutdown()
        for waiter in self._waiters.values():
            waiter['done'].set()
        for waiter in self._service_calls.values():
            waiter['done'].set()
        self._send({'command': 'close'})
        if not self._process.waitForFinished(10000):
            self._process.terminate()
            if not self._process.waitForFinished(1500):
                self._process.kill()
                self._process.waitForFinished(1000)
